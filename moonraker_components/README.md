# Atlas Moonraker component

`atlas.py` is the deliberately thin daemon-to-UI boundary from FD-0002.
It validates and exposes Atlas's atomic status snapshot; it does not import
Atlas, decode telemetry, or recompute a diagnosis.

It registers authenticated HTTP/JSON-RPC endpoints:

- `GET /server/atlas/status` (`server.atlas.status`)
- `GET /server/atlas/incidents` (`server.atlas.incidents`)
- `GET /server/atlas/health` (`server.atlas.health`)
- websocket notification `notify_atlas_status_update`

Install it as `moonraker/components/atlas.py`, then add:

```ini
[atlas]
state_file: ~/.local/state/atlas/status.json
poll_interval: 0.5
stale_after: 15
```

The Atlas service heartbeat defaults to five seconds, so a 15-second stale
threshold tolerates two missed heartbeats without masking a stopped daemon.
