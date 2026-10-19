# Vendored code

Files here are copied **unchanged** from their source, so an update is a plain
copy. Don't edit them; wrap them instead.

| File | Source | Pinned at | Licence |
|---|---|---|---|
| `modbus_sunspec2_reader.py` | franklinwh-modbus `tools/modbus_sunspec2_reader.py` | commit 304b04f (library v0.10.0) | MIT, © the franklinwh-modbus authors |

`modbus_sunspec2_reader.py` is the library's Modbus reader CLI (docs:
franklinwh-modbus `docs/MODBUS_READER_REFERENCE.md`). It is a script, not
part of the installed package, so the bridge carries a copy. The owner said
(2026-10-19) to use it as the reader library "until something better comes
along" (#288). The bridge calls `read_sunspec_device()` and
`print_device_info()` from `franklinwh_bridge.api.reader_api`.
