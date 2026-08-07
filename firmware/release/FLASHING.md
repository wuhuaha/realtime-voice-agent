# ESP32-S3 firmware flashing and provisioning

This bundle contains public firmware images with no Wi-Fi network, Director URL, or bootstrap credential.
The five firmware images deliberately exclude the NVS configuration partition.

Validation status: the CLI host contracts have `34 passed`. Flash, provision, readback, NVS preservation/erase, and
WSS/UDP HIL evidence is tracked in `docs/quality/release-readiness.md`. Rebuilding unchanged runtime source does not
mechanically require another HIL run; endpoint, wire, media lifecycle, transport, or hardware behavior changes do.

## Prerequisites

- Python 3.12.
- `esptool` available to the same Python interpreter (`python -m esptool version`).
- The pinned ESP-IDF 5.5.2 checkout and its Python environment when provisioning, because the tool uses the official
  `nvs_partition_gen.py` and `esp_idf_nvs_partition_gen` module instead of implementing the NVS binary format itself.
- A stable USB connection and an explicit serial port such as `COMx` or `/dev/ttyACM0`.

Run commands from the extracted bundle directory. On Linux, protect any private configuration file with
`chmod 600` before using it.

## Validate and flash

Validate every bundled file against `SHA256SUMS` and `manifest.json`:

```bash
python rva-device-provision.py validate --bundle .
```

Preview the five firmware writes without accessing a device:

```bash
python rva-device-provision.py flash --bundle . --dry-run
```

Flash the public firmware. This command does not erase or write the NVS configuration partition:

```bash
python rva-device-provision.py flash --bundle . --port COMx
```

Use the actual port name on the host. The tool never guesses between native USB and USB-to-UART ports.

## Provision the device

Create a private JSON file outside the repository and extracted bundle:

```json
{
  "schema_version": 1,
  "wifi": {
    "ssid": "your-network",
    "password": "your-password"
  },
  "endpoint": {
    "bootstrap_url": "https://voice.example/v1/session/bootstrap",
    "bootstrap_token": "your-device-bootstrap-token"
  }
}
```

The tool derives `token_origin` from `bootstrap_url`; it does not accept a caller-supplied origin. Preview the
operation without generating or writing a persistent image:

```bash
python rva-device-provision.py provision --bundle . --config /secure/device.json --dry-run
```

Write only the NVS partition and verify it by reading the partition back and comparing its SHA-256:

```bash
python rva-device-provision.py provision --bundle . --config /secure/device.json \
  --idf-path /path/to/esp-idf-v5.5.2 --idf-python /path/to/idf-python --port COMx
```

When run from an activated ESP-IDF shell, `IDF_PATH` and `IDF_PYTHON_ENV_PATH` provide both paths and
`--idf-path`/`--idf-python` may be omitted.

Omit `--config` to enter the same fields through hidden prompts. Passwords and tokens are not accepted as command
line arguments and are not printed. Temporary CSV and NVS image files are removed when the command exits.

## Clear configuration

Preview and then erase only the NVS configuration partition:

```bash
python rva-device-provision.py erase-config --bundle . --dry-run
python rva-device-provision.py erase-config --bundle . --port COMx
```

After erasing configuration, the device returns to its provisioning UI. Re-running the five-image `flash` command
does not clear an existing configuration.

## Security boundary

The current reference firmware does not enable NVS encryption. A party with physical flash-read access can recover
stored Wi-Fi and bootstrap credentials. Use a per-device, least-privilege, revocable bootstrap token; do not use a
shared provider API key. Delete the private JSON file according to the deployment's credential-handling policy after
provisioning. Temporary-file cleanup reduces accidental disclosure but is not secure erasure of SSD storage.
