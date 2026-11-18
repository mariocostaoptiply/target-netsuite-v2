import importlib
import io
import json
import logging
import re
import signal
import sqlite3
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

import pytest
import requests

from target_netsuite_v2.target import TargetNetsuiteV2
from target_netsuite_v2.mapper.base_mapper import BaseMapper, InvalidReferenceError


LINE = {"itemId": "40", "quantity": 2.0, "unitPrice": 3.5, "description": "Test line"}
PO_LINE = {**LINE, "employeeId": "50"}
EXPORT_CUSTOM_PAYLOAD = {
    "customFields": [
        {"name": "approvalStatus", "value": {"id": "1"}},
        {"name": "department", "value": {"id": "70"}},
        {"name": "location", "value": {"id": "80"}},
    ],
}
CASES = [
    ("Vendors", {"vendorName": "New vendor"}, "vendor", {"companyName": "New vendor"}),
    ("Customers", {"companyName": "New customer"}, "customer", {"companyName": "New customer"}),
    ("Accounts", {"name": "New account", "type": "Bank", "subsidiary": ["1"]}, "account", {"acctName": "New account"}),
    ("Items", {"itemNumber": "NEW-ITEM", "type": "InvtPart", "subsidiary": ["1"]}, "inventoryItem", {"itemId": "NEW-ITEM"}),
    ("PurchaseOrders", {"vendorId": "20", "lineItems": [PO_LINE]}, "purchaseOrder", {"entity": {"id": "20"}, "employee": {"id": "50"}}),
    ("Bills", {"vendorId": "20", "lineItems": [LINE]}, "vendorBill", {"entity": {"id": "20"}}),
    ("VendorCredits", {"vendorId": "20", "locationId": "80", "departmentId": "70", "lineItems": [{**LINE, "departmentId": "70", "classId": "60", "locationId": "80"}]}, "vendorCredit", {"entity": {"id": "20"}}),
    ("Invoices", {"customerId": "30", "lineItems": [LINE]}, "invoice", {"entity": {"id": "30"}}),
    ("BillPayments", {"vendorId": "20", "billId": "200", "amount": 7.0}, "vendorPayment", {"apply": {"items": [{"doc": {"id": "200"}, "apply": True, "amount": 7.0}]}}),
    ("InvoicePayments", {"customerId": "30", "invoiceId": "300", "amount": 7.0}, "customerPayment", {"apply": {"items": [{"doc": {"id": "300"}, "apply": True, "amount": 7.0}]}}),
    ("JournalEntries", {"lineItems": [{"accountId": "10", "entryType": "Debit", "debitAmount": 7.0}, {"accountId": "10", "entryType": "Credit", "creditAmount": 7.0}]}, "journalEntry", {"line": {"items": [{"account": {"id": "10"}, "debit": 7.0}, {"account": {"id": "10"}, "credit": 7.0}]}}),
]


@pytest.fixture
def transport(monkeypatch):
    data = {
        "writes": [],
        "contact_queries": [],
        "status": 204,
        "transactions": {
            "VendBill": [{"internalId": "200", "entityid": "20", "subsidiaryId": "1"}],
            "CustInvc": [{"internalId": "300", "entityid": "30", "subsidiaryId": "1"}],
        },
    }
    references = {
        "subsidiary": [{"internalId": "1", "name": "Test subsidiary"}],
        "currency": [{"internalId": "2", "symbol": "EUR", "name": "Euro"}],
        "account": [{"internalId": "10", "name": "Test account"}],
        "employee": [{"internalId": "50", "name": "Test employee", "externalId": "EMP-50", "subsidiaryId": "1"}],
        "classification": [{"internalId": "60", "name": "Test class", "subsidiaryId": "1"}],
        "department": [{"internalId": "70", "name": "Test department", "subsidiaryId": "1"}],
        "location": [
            {"internalId": "80", "name": "Test location", "subsidiaryId": "1"},
            {"internalId": "81", "name": "Other test location", "subsidiaryId": "1"},
        ],
        "vendor": [{"internalId": "20", "name": "Seed vendor", "subsidiaryId": "1"}],
        "customer": [{"internalId": "30", "name": "Seed customer", "subsidiaryId": "1"}],
        "item": [{"internalId": "40", "name": "Test item"}],
    }

    def request(method, url, data=None, **kwargs):
        payload = json.loads(data) if data else None
        response = requests.Response()
        response.status_code = 200
        response.request = requests.Request(method, url, data=data).prepare()
        if "/query/" in url:
            query = payload["q"]
            if "CompanyContactRelationship" in query:
                transport_data["contact_queries"].append((query, kwargs))
                if transport_data.get("contact_exception"):
                    raise transport_data["contact_exception"]
                response.status_code = transport_data.get("contact_status", 200)
                if "contact_response" in transport_data:
                    body = transport_data["contact_response"]
                elif response.status_code != 200:
                    body = {"o:errorDetails": [{"detail": "Contact lookup denied"}]}
                else:
                    # SQLite reserves transaction; the remaining selection SQL is unchanged.
                    cursor = transport_data["contact_db"].execute(query.replace("FROM transaction po", 'FROM "transaction" po'))
                    columns = [column[0] for column in cursor.description]
                    body = {"items": [dict(zip(columns, row)) for row in cursor], "hasMore": False}
                response._content = json.dumps(body).encode()
                return response
            table = re.search(r"\bFROM\s+(\w+)", query, re.IGNORECASE).group(1).lower()
            if "transactionLine" in query or "NextTransactionLineLink" in query:
                items = []
            elif table == "transaction":
                transaction_type = re.search(r"transaction.type = '([^']+)'", query).group(1)
                items = deepcopy(transport_data["transactions"].get(transaction_type, []))
            elif table == "vendor":
                items = deepcopy(transport_data.get("vendors", references["vendor"]))
                id_filter = re.search(r"WHERE id IN \(([^)]+)\)$", query)
                if id_filter:
                    items = [vendor for vendor in items if str(vendor["internalId"]) in id_filter.group(1).split(",")]
            else:
                items = deepcopy(references.get(table, []))
            response._content = json.dumps({"items": items, "hasMore": False}).encode()
        else:
            transport_data["writes"].append((method, url, payload))
            response.status_code = transport_data["status"]
            response.headers["Location"] = f"{url}/900"
            response._content = json.dumps({"o:errorDetails": [{"detail": "Rejected test record"}]}).encode()
        return response

    transport_data = data
    monkeypatch.setattr(requests, "request", request)
    return data


@pytest.fixture
def target_config(request):
    return getattr(request, "param", {})


@pytest.fixture
def contact_db(transport):
    # SDK workers read after fixture writes finish; ingestion waits for their completion.
    database = sqlite3.connect(":memory:", check_same_thread=False)
    database.executescript("""
        CREATE TABLE vendor (id INTEGER PRIMARY KEY);
        CREATE TABLE contact (id INTEGER PRIMARY KEY, isinactive TEXT);
        CREATE TABLE CompanyContactRelationship (company INTEGER, contact INTEGER);
        CREATE TABLE "transaction" (
            id INTEGER PRIMARY KEY, entity INTEGER, type TEXT,
            createddate TEXT, custbody_transaction_contact INTEGER
        );
        INSERT INTO vendor VALUES (20), (21), (999);
        INSERT INTO contact VALUES (100, 'F'), (101, 'F'), (102, 'T'), (200, 'F'), (900, 'F');
        INSERT INTO CompanyContactRelationship VALUES
            (20, 100), (20, 101), (20, 102), (21, 200), (999, 900);
        INSERT INTO "transaction" VALUES
            (1, 20, 'PurchOrd', '2026-11-01', 100),
            (2, 20, 'PurchOrd', '2026-11-02', 100),
            (3, 20, 'PurchOrd', '2026-11-02', 101),
            (4, 20, 'PurchOrd', '2026-11-03', 102),
            (5, 20, 'PurchOrd', '2026-11-04', 200),
            (6, 20, 'PurchOrd', '2026-11-05', NULL),
            (7, 20, 'SalesOrd', '2026-11-06', 100),
            (8, 21, 'PurchOrd', '2026-11-07', 200);
    """)
    transport["contact_db"] = database
    yield database
    database.close()


@pytest.fixture
def target(monkeypatch, tmp_path, transport, request, target_config):
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / ".secrets").mkdir()
    monkeypatch.chdir(work)
    monkeypatch.delenv("STREAMING_JOB", raising=False)
    monkeypatch.delenv("JOB_ID", raising=False)
    monkeypatch.setattr(TargetNetsuiteV2, "incremental_target_state_path", str(tmp_path / "previous.json"))
    previous_handler = signal.getsignal(signal.SIGUSR1)
    state_path = None
    if hasattr(request, "param"):
        state_path = tmp_path / "input-state.json"
        state_path.write_text(json.dumps(request.param))
    instance = TargetNetsuiteV2(config={
        "ns_consumer_key": "synthetic-consumer",
        "ns_consumer_secret": "synthetic-consumer-secret",
        "ns_token_key": "synthetic-token",
        "ns_token_secret": "synthetic-token-secret",
        "ns_account": "synthetic_SB1",
        **target_config,
    }, state=state_path)
    try:
        yield instance
    finally:
        instance._shutdown_requested.set()
        instance.signal_listener_thread.join(timeout=1)
        signal.signal(signal.SIGUSR1, previous_handler)


def ingest(target, stream, record, capsys):
    records = record if isinstance(record, list) else [record]
    messages = [
        {"type": "SCHEMA", "stream": stream, "schema": target.get_sink_class(stream).unified_schema.model_json_schema(), "key_properties": []},
        *({"type": "RECORD", "stream": stream, "record": row} for row in records),
        {"type": "STATE", "value": {}},
    ]
    target.listen(io.StringIO("\n".join(json.dumps(message) for message in messages)))
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.mark.parametrize("stream,fields,endpoint,expected", CASES, ids=[case[0] for case in CASES])
def test_singer_records_reach_netsuite_and_return_state(target, transport, capsys, stream, fields, endpoint, expected):
    record = {"externalId": f"synthetic-{stream}", **deepcopy(fields)}
    if stream not in {"Accounts", "Items"}:
        record.update(subsidiaryId="1", currency="EUR")
    state = ingest(target, stream, record, capsys)

    method, url, payload = transport["writes"][0]
    assert method == "POST"
    assert url.endswith(f"/record/v1/{endpoint}")
    for key, value in expected.items():
        assert payload[key] == value
    if stream in {"PurchaseOrders", "Bills", "VendorCredits", "Invoices"}:
        assert payload["item"]["items"][0]["item"] == {"id": "40"}
        assert payload["item"]["items"][0]["quantity"] == 2.0
    bookmark = state["bookmarks"][stream][-1]
    assert bookmark["success"] is True
    assert bookmark["id"] == "900"
    assert bookmark["externalId"] == record["externalId"]


def test_existing_purchase_order_is_patched(target, transport, capsys):
    transport["transactions"]["PurchOrd"] = [{"internalId": "100", "externalId": "synthetic-po", "subsidiaryId": "1"}]
    state = ingest(target, "PurchaseOrders", {"externalId": "synthetic-po", "vendorId": "20", "lineItems": [PO_LINE]}, capsys)

    method, url, payload = transport["writes"][0]
    assert method == "PATCH"
    assert url.endswith("/purchaseOrder/100")
    assert payload["item"]["items"][0]["rate"] == 3.5
    assert state["summary"]["PurchaseOrders"]["updated"] == 1
    assert state["bookmarks"]["PurchaseOrders"][-1]["id"] == "100"


def test_rejected_purchase_order_is_reported_as_failed(target, transport, capsys):
    transport["status"] = 400
    state = ingest(target, "PurchaseOrders", {"externalId": "synthetic-rejected", "vendorId": "20", "lineItems": [PO_LINE]}, capsys)

    bookmark = state["bookmarks"]["PurchaseOrders"][-1]
    assert bookmark["success"] is False
    assert "Rejected test record" in bookmark["error"]
    assert state["summary"]["PurchaseOrders"]["fail"] == 1


def test_missing_vendor_emits_explicit_failure_without_writing(target, transport, capsys):
    record = {
        "id": "synthetic-order",
        "externalId": "synthetic-missing-vendor",
        "vendorId": "99999",
        "lineItems": [PO_LINE],
    }
    state = ingest(target, "PurchaseOrders", record, capsys)

    bookmark = state["bookmarks"]["PurchaseOrders"][-1]
    assert bookmark["success"] is False
    assert bookmark["id"] == record["id"]
    assert bookmark["externalId"] == record["externalId"]
    assert "vendor" in bookmark["error"]
    assert "99999" in bookmark["error"]
    assert transport["writes"] == []
    assert state["summary"]["PurchaseOrders"]["fail"] == 1
    assert state["summary"]["PurchaseOrders"]["success"] == 0


@pytest.mark.parametrize("target", [{
    "bookmarks": {"Vendors": [{"id": "20", "externalId": "synthetic-vendor", "success": True}]},
    "summary": {"Vendors": {"success": 1, "fail": 0, "existing": 0, "updated": 0}},
}], indirect=True)
def test_state_file_bookmarks_survive_new_delivery(target, capsys):
    state = ingest(target, "PurchaseOrders", {"externalId": "synthetic-resume", "vendorId": "20", "lineItems": [PO_LINE]}, capsys)
    assert state["bookmarks"]["Vendors"] == [{"id": "20", "externalId": "synthetic-vendor", "success": True}]
    assert state["bookmarks"]["PurchaseOrders"][-1]["success"] is True


@pytest.mark.parametrize("target_config", [
    {},
    {"export_target_custom_payload": EXPORT_CUSTOM_PAYLOAD, "send_locationId": "81"},
], indirect=True)
def test_real_time_purchase_order_returns_delivery_state(target, transport, monkeypatch, target_config):
    sdk_lambda = importlib.import_module("hotglue_singer_sdk.target_sdk.lambda")
    handler = importlib.import_module("target_netsuite_v2.lambda").real_time_handler
    config_paths = []

    def run_offline(real_time):
        config_paths.append(Path(real_time.config_file_path))
        assert real_time.command == "target-netsuite-v2"
        with open(real_time.singer_file_path) as source, open(real_time.state_file_path, "w") as output, redirect_stdout(output):
            offline_target = TargetNetsuiteV2(config=json.loads(Path(real_time.config_file_path).read_text()), parse_env_config=False)
            offline_target.listen(source)
        return {"tracebackInLogs": False, "logs": ""}

    monkeypatch.setattr(sdk_lambda.RealTime, "run", run_offline)
    schema = {"type": "SCHEMA", "stream": "PurchaseOrders", "schema": target.get_sink_class("PurchaseOrders").unified_schema.model_json_schema(), "key_properties": []}
    record = {"type": "RECORD", "stream": "PurchaseOrders", "record": {"externalId": "synthetic-realtime", "vendorId": "20", "lineItems": [PO_LINE]}}
    try:
        result = handler(dict(target.config), "PurchaseOrders", json.dumps(schema), json.dumps(record), logging.getLogger("test"))
        bookmark = result["state"]["bookmarks"]["PurchaseOrders"][-1]
        assert bookmark["id"] == "900"
        assert bookmark["success"] is True
        assert transport["writes"][0][1].endswith("/purchaseOrder")
        if target_config:
            assert transport["writes"][0][2]["approvalStatus"] == {"id": "1"}
            assert transport["writes"][0][2]["department"] == {"id": "70"}
            assert transport["writes"][0][2]["location"] == {"id": "80"}
            assert transport["writes"][0][2]["item"]["items"][0]["location"] == {"id": "81"}
    finally:
        for path in config_paths:
            path.unlink(missing_ok=True)


@pytest.mark.parametrize("target_config", [{}, {"vendor_transaction_contact": False}], indirect=True)
def test_disabled_vendor_contact_lookup_preserves_supplied_contact(target, transport, capsys):
    record = {
        "externalId": "synthetic-disabled-contact", "vendorId": "20", "lineItems": [PO_LINE],
        "customFields": [{"name": "custbody_transaction_contact", "value": {"id": "777"}}],
    }
    state = ingest(target, "PurchaseOrders", record, capsys)
    assert transport["contact_queries"] == []
    assert transport["writes"][0][2]["custbody_transaction_contact"] == {"id": "777"}
    assert state["bookmarks"]["PurchaseOrders"][-1]["success"] is True


@pytest.mark.parametrize("target_config", [{"vendor_transaction_contact": True}], indirect=True)
@pytest.mark.usefixtures("contact_db")
class TestVendorTransactionContact:
    def test_latest_valid_po_contact_replaces_stale_contact_and_keeps_other_fields(self, target, transport, capsys):
        record = {
            "externalId": "synthetic-live-contact", "vendorId": "20", "lineItems": [PO_LINE],
            "customFields": [
                {"name": "approvalStatus", "value": {"id": "1"}},
                {"name": "custbody_transaction_contact", "value": {"id": "777"}},
            ],
        }
        state = ingest(target, "PurchaseOrders", record, capsys)
        payload = transport["writes"][0][2]
        assert payload["entity"] == {"id": "20"}
        assert payload["custbody_transaction_contact"] == {"id": "101"}
        assert payload["approvalStatus"] == {"id": "1"}
        assert state["bookmarks"]["PurchaseOrders"][-1]["success"] is True
        assert len(transport["contact_queries"]) == 1
        assert transport["contact_queries"][0][1]["timeout"] == (10, 60)

    def test_sole_active_contact_is_used_without_po_history(self, target, transport, contact_db, capsys):
        contact_db.execute('DELETE FROM "transaction" WHERE entity = 20')
        contact_db.execute("DELETE FROM CompanyContactRelationship WHERE company = 20 AND contact = 101")
        ingest(target, "PurchaseOrders", {
            "externalId": "synthetic-sole-contact", "vendorName": "Seed vendor", "lineItems": [PO_LINE],
        }, capsys)
        assert transport["writes"][0][2]["custbody_transaction_contact"] == {"id": "100"}

    @pytest.mark.parametrize("remove_contacts", [False, True])
    def test_ambiguous_or_missing_contact_is_omitted(self, target, transport, contact_db, capsys, remove_contacts):
        contact_db.execute('DELETE FROM "transaction" WHERE entity = 20')
        if remove_contacts:
            contact_db.execute("DELETE FROM CompanyContactRelationship WHERE company = 20")
        state = ingest(target, "PurchaseOrders", {
            "externalId": "synthetic-no-contact", "vendorId": "20", "lineItems": [PO_LINE],
            "customFields": [{"name": "custbody_transaction_contact", "value": {"id": "777"}}],
        }, capsys)
        assert "custbody_transaction_contact" not in transport["writes"][0][2]
        assert state["bookmarks"]["PurchaseOrders"][-1]["success"] is True

    def test_batch_contacts_stay_with_their_resolved_vendor(self, target, transport, capsys):
        transport["vendors"] = [
            {"internalId": "20", "name": "Seed vendor"},
            {"internalId": "21", "name": "Other vendor"},
        ]
        state = ingest(target, "PurchaseOrders", [
            {"externalId": "synthetic-vendor-20", "vendorId": "20", "lineItems": [PO_LINE]},
            {"externalId": "synthetic-vendor-21", "vendorId": "21", "lineItems": [PO_LINE]},
        ], capsys)
        assert [
            (payload["entity"]["id"], payload["custbody_transaction_contact"]["id"])
            for _, _, payload in transport["writes"]
        ] == [("20", "101"), ("21", "200")]
        assert len(transport["contact_queries"]) == 1
        assert state["summary"]["PurchaseOrders"]["success"] == 2

    def test_existing_purchase_order_receives_current_contact(self, target, transport, capsys):
        transport["transactions"]["PurchOrd"] = [{"internalId": "1000", "externalId": "synthetic-contact-update", "subsidiaryId": "1"}]
        state = ingest(target, "PurchaseOrders", {
            "externalId": "synthetic-contact-update", "vendorId": "20", "lineItems": [PO_LINE],
        }, capsys)
        method, url, payload = transport["writes"][0]
        assert method == "PATCH"
        assert url.endswith("/purchaseOrder/1000")
        assert payload["custbody_transaction_contact"] == {"id": "101"}
        assert state["summary"]["PurchaseOrders"]["updated"] == 1

    def test_contact_flag_does_not_enrich_bills(self, target, transport, capsys):
        state = ingest(target, "Bills", {
            "externalId": "synthetic-bill-no-contact", "vendorId": "20", "lineItems": [LINE],
        }, capsys)
        assert transport["contact_queries"] == []
        assert "custbody_transaction_contact" not in transport["writes"][0][2]
        assert state["bookmarks"]["Bills"][-1]["success"] is True

    @pytest.mark.parametrize("status", [401, 403, 429, 500])
    def test_contact_http_failure_blocks_po_and_emits_failure_state(self, target, transport, capsys, status):
        transport["contact_status"] = status
        state = ingest(target, "PurchaseOrders", {
            "externalId": "synthetic-contact-error", "vendorId": "20", "lineItems": [PO_LINE],
        }, capsys)
        bookmark = state["bookmarks"]["PurchaseOrders"][-1]
        assert bookmark["success"] is False
        assert bookmark["externalId"] == "synthetic-contact-error"
        assert "Contact lookup denied" in bookmark["error"]
        assert transport["writes"] == []
        assert state["summary"]["PurchaseOrders"]["fail"] == 1

    def test_contact_timeout_blocks_po(self, target, transport, capsys):
        transport["contact_exception"] = requests.Timeout("Synthetic contact timeout")
        state = ingest(target, "PurchaseOrders", {
            "externalId": "synthetic-contact-timeout", "vendorId": "20", "lineItems": [PO_LINE],
        }, capsys)
        assert transport["writes"] == []
        assert state["bookmarks"]["PurchaseOrders"][-1]["success"] is False
        assert "Synthetic contact timeout" in state["bookmarks"]["PurchaseOrders"][-1]["error"]

    def test_malformed_http_error_blocks_po(self, target, transport, capsys):
        transport["contact_status"] = 403
        transport["contact_response"] = None
        state = ingest(target, "PurchaseOrders", {
            "externalId": "synthetic-malformed-http-error", "vendorId": "20", "lineItems": [PO_LINE],
        }, capsys)
        assert transport["writes"] == []
        assert state["bookmarks"]["PurchaseOrders"][-1]["success"] is False

    @pytest.mark.parametrize("body", [
        None,
        {"items": None},
        {"items": []},
        {"items": [None]},
        {"items": [{"vendor_id": "21", "contact_id": "200"}]},
        {"items": [{"vendor_id": "20", "contact_id": "not-an-id"}]},
        {"items": [{"vendor_id": "20", "contact_id": "101"}] * 2},
        {"items": [{"vendor_id": "20", "contact_id": "101"}], "hasMore": True},
    ])
    def test_incomplete_or_invalid_contact_response_never_writes_po(self, target, transport, capsys, body):
        transport["contact_response"] = body
        state = ingest(target, "PurchaseOrders", {
            "externalId": "synthetic-invalid-contact", "vendorId": "20", "lineItems": [PO_LINE],
        }, capsys)
        assert transport["writes"] == []
        assert state["bookmarks"]["PurchaseOrders"][-1]["success"] is False
        assert state["summary"]["PurchaseOrders"]["fail"] == 1

    def test_large_vendor_lookup_handles_final_short_batch(self, target, transport, contact_db):
        vendor_ids = {str(value) for value in range(1, 1002)}
        contact_db.executemany("INSERT OR IGNORE INTO vendor VALUES (?)", [(value,) for value in vendor_ids])
        success, error, contacts = target.suite_talk_client.get_vendor_transaction_contacts(vendor_ids)
        assert success, error
        assert contacts.keys() == vendor_ids
        assert contacts["20"] == "101"
        assert contacts["21"] == "200"
        assert contacts["1"] is None
        assert len(transport["contact_queries"]) == 2

    def test_non_numeric_vendor_cannot_reach_contact_query(self, target, transport):
        success, error, contacts = target.suite_talk_client.get_vendor_transaction_contacts({"20 OR 1=1"})
        assert success is False
        assert "numeric" in error
        assert contacts == {}
        assert transport["contact_queries"] == []


@pytest.mark.parametrize("target_config", [
    {"export_target_custom_payload": EXPORT_CUSTOM_PAYLOAD},
], indirect=True)
class TestExportTargetCustomPayload:
    def test_headers_merge_without_losing_record_fields_or_leaking_between_orders(self, target, transport, capsys):
        state = ingest(target, "PurchaseOrders", [
            {
                "externalId": "synthetic-extra-first", "vendorId": "20", "lineItems": [PO_LINE],
                "customFields": [
                    {"name": "approvalStatus", "value": {"id": "9"}},
                    {"name": "custbody_test_note", "value": "first order only"},
                ],
            },
            {"externalId": "synthetic-extra-second", "vendorId": "20", "lineItems": [PO_LINE], "customFields": None},
        ], capsys)
        first, second = [payload for _, _, payload in transport["writes"]]
        for payload in (first, second):
            assert payload["approvalStatus"] == {"id": "1"}
            assert payload["department"] == {"id": "70"}
            assert payload["location"] == {"id": "80"}
            assert payload["entity"] == {"id": "20"}
            assert payload["item"]["items"][0]["quantity"] == 2.0
            assert payload["item"]["items"][0]["rate"] == 3.5
        assert first["custbody_test_note"] == "first order only"
        assert "custbody_test_note" not in second
        assert state["summary"]["PurchaseOrders"]["success"] == 2

    def test_enrichment_also_applies_to_updates(self, target, transport, capsys):
        transport["transactions"]["PurchOrd"] = [{"internalId": "100", "externalId": "synthetic-extra-update", "subsidiaryId": "1"}]
        state = ingest(target, "PurchaseOrders", {
            "externalId": "synthetic-extra-update", "vendorId": "20", "lineItems": [PO_LINE],
        }, capsys)
        method, url, payload = transport["writes"][0]
        assert method == "PATCH"
        assert url.endswith("/purchaseOrder/100")
        assert payload["approvalStatus"] == {"id": "1"}
        assert payload["department"] == {"id": "70"}
        assert payload["location"] == {"id": "80"}
        assert state["bookmarks"]["PurchaseOrders"][-1]["success"] is True

    def test_other_streams_are_not_enriched(self, target, transport, capsys):
        state = ingest(target, "Bills", {
            "externalId": "synthetic-bill-no-extra", "vendorId": "20", "lineItems": [LINE],
        }, capsys)
        payload = transport["writes"][0][2]
        assert not {"approvalStatus", "department", "location"} & payload.keys()
        assert state["bookmarks"]["Bills"][-1]["success"] is True

    def test_changed_config_applies_after_state_restore(self, target, transport, capsys, tmp_path):
        record = {"externalId": "synthetic-extra-replay", "vendorId": "20", "lineItems": [PO_LINE]}
        state = ingest(target, "PurchaseOrders", record, capsys)
        state_path = tmp_path / "enriched-state.json"
        state_path.write_text(json.dumps(state))
        config = deepcopy(dict(target.config))
        config["export_target_custom_payload"]["customFields"][0]["value"]["id"] = "2"
        transport["transactions"]["PurchOrd"] = [{"internalId": "900", "externalId": record["externalId"], "subsidiaryId": "1"}]
        resumed = TargetNetsuiteV2(config=config, parse_env_config=False, state=str(state_path))
        state = ingest(resumed, "PurchaseOrders", record, capsys)
        assert transport["writes"][-1][0] == "PATCH"
        assert transport["writes"][-1][2]["approvalStatus"] == {"id": "2"}
        assert state["summary"]["PurchaseOrders"]["updated"] == 1


@pytest.mark.parametrize("target_config", [{
    "export_target_custom_payload": {"vendorId": "20", "description": "Configured memo"},
}], indirect=True)
def test_extra_record_fields_apply_before_reference_lookup(target, transport, capsys):
    state = ingest(target, "PurchaseOrders", {
        "externalId": "synthetic-extra-vendor", "vendorId": "99999",
        "description": "Incoming memo", "lineItems": [PO_LINE],
    }, capsys)
    payload = transport["writes"][0][2]
    assert payload["entity"] == {"id": "20"}
    assert payload["memo"] == "Configured memo"
    assert state["bookmarks"]["PurchaseOrders"][-1]["success"] is True


@pytest.mark.parametrize("target_config", [
    {"export_target_custom_payload": {"customFields": fields}}
    for fields in ("invalid", ["invalid"])
], indirect=True)
def test_invalid_configured_fields_fail_without_writing(target, transport, capsys):
    state = ingest(target, "PurchaseOrders", {
        "externalId": "synthetic-invalid-extra", "vendorId": "20", "lineItems": [PO_LINE],
    }, capsys)
    assert transport["writes"] == []
    bookmark = state["bookmarks"]["PurchaseOrders"][-1]
    assert bookmark["success"] is False
    assert bookmark["externalId"] == "synthetic-invalid-extra"
    assert "customFields" in bookmark["error"]


@pytest.mark.parametrize("target_config", [{
    "vendor_transaction_contact": True,
    "export_target_custom_payload": {
        "customFields": [{"name": "custbody_transaction_contact", "value": {"id": "777"}}],
    },
}], indirect=True)
@pytest.mark.usefixtures("contact_db")
def test_fetched_contact_overrides_configured_contact(target, transport, capsys):
    state = ingest(target, "PurchaseOrders", {
        "externalId": "synthetic-extra-contact", "vendorId": "20", "lineItems": [PO_LINE],
    }, capsys)
    assert transport["writes"][0][2]["custbody_transaction_contact"] == {"id": "101"}
    assert state["bookmarks"]["PurchaseOrders"][-1]["success"] is True


@pytest.mark.parametrize("target_config", [{
    "send_locationId": "81", "export_target_custom_payload": EXPORT_CUSTOM_PAYLOAD,
}], indirect=True)
@pytest.mark.parametrize("existing", [False, True])
def test_configured_location_overrides_all_po_lines_without_changing_header(target, transport, capsys, existing):
    external_id = "synthetic-line-location"
    if existing:
        transport["transactions"]["PurchOrd"] = [{"internalId": "100", "externalId": external_id, "subsidiaryId": "1"}]
    state = ingest(target, "PurchaseOrders", {
        "externalId": external_id, "vendorId": "20",
        "lineItems": [
            {**PO_LINE, "locationId": "80", "locationName": "Test location"},
            {**LINE, "description": "Second line"},
        ],
    }, capsys)
    method, _, payload = transport["writes"][0]
    assert method == ("PATCH" if existing else "POST")
    assert [line["location"] for line in payload["item"]["items"]] == [{"id": "81"}, {"id": "81"}]
    assert [line["quantity"] for line in payload["item"]["items"]] == [2.0, 2.0]
    assert payload["location"] == {"id": "80"}
    assert state["bookmarks"]["PurchaseOrders"][-1]["success"] is True


@pytest.mark.parametrize("target_config", [{}, {"send_locationId": ""}], indirect=True)
def test_unconfigured_line_location_keeps_incoming_locations(target, transport, capsys):
    state = ingest(target, "PurchaseOrders", {
        "externalId": "synthetic-no-line-location", "vendorId": "20",
        "lineItems": [{**LINE, "locationId": "80"}, {**LINE, "description": "No location"}],
    }, capsys)
    lines = transport["writes"][0][2]["item"]["items"]
    assert lines[0]["location"] == {"id": "80"}
    assert "location" not in lines[1]
    assert state["bookmarks"]["PurchaseOrders"][-1]["success"] is True


@pytest.mark.parametrize("target_config", [{"send_locationId": "81"}], indirect=True)
def test_po_line_location_config_does_not_override_bill_locations(target, transport, capsys):
    state = ingest(target, "Bills", {
        "externalId": "synthetic-bill-location", "vendorId": "20",
        "lineItems": [{**LINE, "locationId": "80"}],
    }, capsys)
    assert transport["writes"][0][2]["item"]["items"][0]["location"] == {"id": "80"}
    assert state["bookmarks"]["Bills"][-1]["success"] is True


@pytest.mark.parametrize("target_config", [{"send_locationId": "99999"}], indirect=True)
def test_unknown_configured_location_cannot_fall_back_to_incoming_name(target, transport, capsys):
    state = ingest(target, "PurchaseOrders", {
        "externalId": "synthetic-unknown-location", "vendorId": "20",
        "lineItems": [{**PO_LINE, "locationName": "Test location"}],
    }, capsys)
    assert transport["writes"] == []
    bookmark = state["bookmarks"]["PurchaseOrders"][-1]
    assert bookmark["success"] is False
    assert "99999" in bookmark["error"]


@pytest.mark.parametrize("target_config", [
    {"send_locationId": "81", "export_target_custom_payload": {"lineItems": lines}}
    for lines in ("invalid", ["invalid"])
], indirect=True)
def test_location_enrichment_preserves_invalid_line_failure_state(target, transport, capsys):
    state = ingest(target, "PurchaseOrders", {
        "externalId": "synthetic-invalid-location-lines", "vendorId": "20", "lineItems": [PO_LINE],
    }, capsys)
    assert transport["writes"] == []
    bookmark = state["bookmarks"]["PurchaseOrders"][-1]
    assert bookmark["success"] is False
    assert bookmark["externalId"] == "synthetic-invalid-location-lines"


@pytest.mark.parametrize("target_config", [{
    "send_locationId": "81",
    "export_target_custom_payload": {"lineItems": [{**PO_LINE, "locationId": "80"}]},
}], indirect=True)
def test_location_enrichment_does_not_mutate_configured_lines(target, transport, capsys):
    state = ingest(target, "PurchaseOrders", {
        "externalId": "synthetic-configured-location-lines", "vendorId": "20",
    }, capsys)
    assert transport["writes"][0][2]["item"]["items"][0]["location"] == {"id": "81"}
    assert target.config["export_target_custom_payload"]["lineItems"][0]["locationId"] == "80"
    assert state["bookmarks"]["PurchaseOrders"][-1]["success"] is True


@pytest.mark.parametrize("existing", [False, True])
def test_employee_is_optional_with_a_known_po_subsidiary(target, transport, capsys, existing):
    record = {"externalId": "synthetic-optional-employee", "vendorId": "20", "lineItems": [LINE]}
    if existing:
        transport["transactions"]["PurchOrd"] = [{"internalId": "100", "externalId": record["externalId"], "subsidiaryId": "1"}]
    else:
        record["subsidiaryId"] = "1"
    state = ingest(target, "PurchaseOrders", record, capsys)
    method, _, payload = transport["writes"][0]
    assert method == ("PATCH" if existing else "POST")
    assert "employee" not in payload
    assert "employee" not in payload["item"]["items"][0]
    assert state["bookmarks"]["PurchaseOrders"][-1]["success"] is True


@pytest.mark.parametrize("employee_fields,known", [
    ({"employeeId": "50"}, True),
    ({"employeeName": "Test employee"}, True),
    ({"employeeNumber": "EMP-50"}, True),
    ({"employeeId": "99999"}, False),
    ({"employeeName": "Unknown employee"}, False),
    ({"employeeNumber": "UNKNOWN-EMPLOYEE"}, False),
])
def test_supplied_po_employee_must_resolve(target, transport, capsys, employee_fields, known):
    external_id = "synthetic-supplied-employee"
    transport["transactions"]["PurchOrd"] = [{"internalId": "100", "externalId": external_id, "subsidiaryId": "1"}]
    state = ingest(target, "PurchaseOrders", {
        "externalId": external_id, "vendorId": "20", "lineItems": [{**LINE, **employee_fields}],
    }, capsys)
    bookmark = state["bookmarks"]["PurchaseOrders"][-1]
    if known:
        assert transport["writes"][0][0] == "PATCH"
        assert transport["writes"][0][2]["employee"] == {"id": "50"}
        assert bookmark["success"] is True
    else:
        assert transport["writes"] == []
        assert bookmark["success"] is False
        assert bookmark["externalId"] == external_id
        assert "employee" in bookmark["error"]


@pytest.mark.parametrize("lookup_arg,record_field", [
    ("id_field", "employeeId"),
    ("name_field", "employeeName"),
    ("external_id_field", "employeeNumber"),
    ("entity_id_field", "vendorNumber"),
    ("item_id_field", "itemNumber"),
    ("tran_id_field", "billNumber"),
    ("number_field", "accountNumber"),
])
@pytest.mark.parametrize("provided", [False, True])
def test_reference_field_names_are_not_reference_values(lookup_arg, record_field, provided):
    record = {record_field: "missing"} if provided else {}
    mapper = BaseMapper(record, "PurchaseOrders", {"PurchaseOrders": []})
    options = {"id_field": "employeeId", "name_field": "employeeName", "subsidiary_scope": "1", lookup_arg: record_field}
    if provided:
        with pytest.raises(InvalidReferenceError):
            mapper._find_reference_by_id_or_ref([], **options)
    else:
        assert mapper._find_reference_by_id_or_ref([], **options) is None
