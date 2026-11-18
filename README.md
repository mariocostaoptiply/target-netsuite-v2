# target-netsuite-v2

`target-netsuite-v2` is a Singer target for netsuite-v2.

Built with `hotglue-singer-sdk==1.0.38`, matching the release locked by
`Optiply/target-picqer`. Both CLI entry points (`target-netsuite-v2` and
`target-netsuite`) and the real-time handler use this SDK.

## Installation

- [ ] `Developer TODO:` Update the below as needed to correctly describe the install procedure. For instance, if you do not have a PyPi repo, or if you want users to directly install from your git repo, you can modify this step as appropriate.

```bash
pipx install target-netsuite-v2
```

## Configuration

### Accepted Config Options

- [ ] `Developer TODO:` Provide a list of config options accepted by the target.

A full list of supported settings and capabilities for this
target is available by running:

```bash
target-netsuite-v2 --about
```

`vendor_transaction_contact` (default `false`): fetch PO contacts directly from
NetSuite using the latest valid PO contact or sole active vendor contact.
Replaces incoming `custbody_transaction_contact`; omits it if none is found.
Lookup errors block PO writes and emit `success: false`.

`export_target_custom_payload` (default `{}`): overlays PO fields before mapping.
`customFields` are appended; config wins conflicts. Contact lookup still wins last.

`send_locationId`: when non-empty, overrides every PO line location; header location is unchanged.

### Configure using environment variables

This Singer target will automatically import any environment variables within the working directory's
`.env` if the `--config=ENV` is provided, such that config values will be considered if a matching
environment variable is set either in the terminal context or in the `.env` file.

### Source Authentication and Authorization

- [ ] `Developer TODO:` If your target requires special access on the source system, or any special authentication requirements, provide those here.

## Usage

You can easily run `target-netsuite-v2` by itself or in a pipeline using [Meltano](https://meltano.com/).

Mapping failures emit `success: false`, error details and available identifiers in
the record bookmark, and increment the stream summary's `fail` counter.

PO employee mapping is optional; supplied employee references must resolve.

### Executing the Target Directly

```bash
target-netsuite-v2 --version
target-netsuite-v2 --help
# Test using the "Carbon Intensity" sample:
tap-carbon-intensity | target-netsuite-v2 --config /path/to/target-netsuite-v2-config.json
```

## Developer Resources

- [ ] `Developer TODO:` As a first step, scan the entire project for the text "`TODO:`" and complete any recommended steps, deleting the "TODO" references once completed.

### Initialize your Development Environment

```bash
pipx install poetry
poetry install
```

### Run Offline Tests

The compatibility tests exercise all entity streams, purchase-order updates and
failures, state restoration, and the real-time handler. HTTP is mocked; real-time
processing runs in-process instead of spawning the CLI. No NetSuite account is used.

```bash
poetry run pytest target_netsuite_v2/tests/test_sdk_compatibility.py -q
```

You can also test the `target-netsuite-v2` CLI interface directly using `poetry run`:

```bash
poetry run target-netsuite-v2 --help
```

### Testing with [Meltano](https://meltano.com/)

_**Note:** This target will work in any Singer environment and does not require Meltano.
Examples here are for convenience and to streamline end-to-end orchestration scenarios._

Your project comes with a custom `meltano.yml` project file already created. Open the `meltano.yml` and follow any _"TODO"_ items listed in
the file.

Next, install Meltano (if you haven't already) and any needed plugins:

```bash
# Install meltano
pipx install meltano
# Initialize meltano within this directory
cd target-netsuite-v2
meltano install
```

Now you can test and orchestrate using Meltano:

```bash
# Test invocation:
meltano invoke target-netsuite-v2 --version
# OR run a test `elt` pipeline with the Carbon Intensity sample tap:
meltano elt tap-carbon-intensity target-netsuite-v2
```

### SDK Dev Guide

See the [dev guide](https://sdk.meltano.com/en/latest/dev_guide.html) for more instructions on how to use the Meltano SDK to
develop your own Singer taps and targets.
