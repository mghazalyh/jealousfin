# Application revisions and native recovery (ABI 1)
Build 1021 adds a single trial journal for runtime, Jellyfin adapter, dynamic UI,
and configuration, plus a permanent native recovery screen and iOS app icon.
Use get_application_revision to obtain the four-component identity and trial
metadata. validate_patch_bundle accepts manifest JSON and checks its ABI,
source limits, interpreter fixtures and layout/configuration rules. It is not
a live playback test. begin_application_trial takes the same manifest, the
exact identity JSON as expected, and seconds (30–3600). The prior complete
source snapshot is persisted before any source pointer changes. Activation
stops the existing stream and reconnects, if auto-login is enabled. It is
not a seamless playback swap. A supplied finite acceptance plan runs on the
device; acceptance requires its completed matching-revision passing results.
finish_application_trial takes id, outcome=accept|rollback and approval=explicit
for accept. A trial without an acceptance plan requires operator confirmation
and is not automatically certified. Expiry or relaunch restores prior sources.
An archived manifest can be restored through restore_application_revision
as another trial. Individual source mutation commands are blocked in trials.
get_command_schema documents new engineering commands; get_configuration_schema
lists configuration sections and bounds; get_native_defaults returns the
baseline policy. Older commands remain discoverable via get_native_capabilities.

Native recovery is reachable via More > Native recovery & LAN access.
It copies active source stores and recovery journals to a recovery directory,
then resets the sources and pauses campaign automation. Account settings,
health, receipts and immutable archives are retained. The marker prevents
bundled baseline reinstallation during safe mode. Exit safe mode explicitly
before installing new sources. LAN authentication is optional and configured
only in the native recovery screen. Existing installations retain legacy LAN
access until the user enables it. Authentication requires Authorization:
Bearer <token> for commands and private reads, with /health public. HTTP is
not encrypted; operate on a trusted private network. The token is not exposed
in telemetry or capability snapshots.

The gateway configuration supports explicit private IPv4 port pairs, route
prefix, ticket response key, and request timeout; HTTPS stays same-origin.
Reconnect to reprobe gateway availability after changing the profile. No
arbitrary destination host or token forwarding is added. The Settings screen
can validate and reload a database connection without restarting. Pending
telemetry/QA/archive files block cross-repository switches.

The iOS AppIcon asset catalogue contains the generated teal/navy/gold artwork
with opaque square master and legacy iPhone/iPad sizes. iOS applies its own
corner mask. The existing iOS 15 minimum, bundle identifier, signing and
accepted Xcode workflow are unchanged.

Remaining boundaries: custom categories beyond the existing four presets,
full theme/property schemas, configurable audio interruption and lifecycle
behavior, retention for immutable archives and receipts, transport encryption
and fully generalized codec profiles are not delivered by this revision.
The current diagnostics export contains selected state and recent events,
not a full filesystem dump. Physical acceptance on iOS 15.6.1 remains required.
