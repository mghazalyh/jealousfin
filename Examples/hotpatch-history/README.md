# Live hot patch source history (2026-11-25)

Portable copies preserve two retrieved configuration revisions and the latest live fullscreen UI revision. These are source fixtures, not automatically activated release resources. Native defaults implement faster recovery and compact controls, including the unwired brightness preview.

- Startup timeout default: 15 s for new settings; saved preferences remain intact.
- Stall timeout: 10 s; failure retry delay: 5 s; advance on stall.
- Live configuration uses one-minute rotation; existing configurable rotation defaults/preferences are not overwritten.
- Fullscreen footer cap: 25%; opacity: 65%; rotation control precedes player pause.
- Brightness preview: -10 through +10, reset to zero; no video or screen effect. Native preview lives in the Surf replacement region to prevent duplication with the live custom toolbar.
- Live UI revision: fullscreen-brightness-preview-20261125; previous UI revision: fullscreen-rotation-first-20261125 remains in the device version store.
- Live adapter: iptv-hiatus-snapshot-1795612090. Temporary channel IDs, deadlines and attempt history are not bundled. Generate a fresh offline adapter using Server/iptv/make_policy_snapshot.py. The SQLite-aware gateway is the long-term solution. A snapshot is not continuous synchronization.

## Upgrade and rollback

Every different IPA version/build starts a new patch epoch. Startup discards prior active overrides, local patch histories, trials and recovery journals before loading the incoming bundled baseline. Relaunches of the same version/build retain new hot patches and rollback history. The first launch reconciliation may record one new baseline checkpoint. Settings, credentials, channel health, rotation, receipts and telemetry are preserved. Old remote archives are not deleted from the repository but cannot be restored through the new release-scoped archive path. A Git commit or example file is not a device rollback archive.

No private device archive, credentials or live channel IDs are bundled. Before installing a new IPA, ensure all intended active patch behavior is promoted and the SQLite-aware gateway is enforcing channel policy; clearing a legacy static adapter otherwise removes its temporary exclusions. Do not mark experimental snapshots known-good solely because activation succeeded.
