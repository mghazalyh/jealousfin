# Accepted Jealousfin IPA release baseline

Build 1015 was confirmed installed and working on the owner's physical iPad using TrollStore on 2026-11-25 SGT. This is the reference release process for subsequent public Jealousfin IPAs.

- Release: https://github.com/mghazalyh/jealousfin/releases/tag/v0.1.13-build1015-1
- Successful Actions run: 45481664166
- Source commit: c342443ec29abb0f138f21f2bf3084a0f756fe3d
- Primary runner: macos-15-intel
- Xcode 16.4 (16F6), iPhoneOS SDK 18.5, iOS deployment target 15.0
- ARM64, iPhone/iPad, bundle identifier com.example.jealousfin
- Shared package script: Scripts/package-public-ipa.sh
- Sign using the existing PlayTorrio-reference ad hoc signing procedure; verify after copying and extraction.
- Retain both signed and unsigned assets because the owner did not identify which variant was installed.
- Signed IPA SHA256: 9bafeccad89612faccfc02089e587be88709cfd40fa2a16f2e9495f28e9858f2
- Unsigned IPA SHA256: 08efee9a42e140b5d96dca0d8cb93d063a2467939c3e82ae60a872e607b34dff
- Preserve full policy and runtime regression gates and separate Ubuntu publisher.
- Publish successful public release runs as stable/latest, not prereleases. Verify uploaded asset sizes.
- Do not silently substitute a different Xcode/SDK. A runner lacking the accepted toolchain must fail clearly.
- Never include private defaults, credentials, distribution certificates or provisioning profiles in public IPAs.
- Do not change packaging, bundle identity or toolchain without explicit review and renewed device acceptance.

The build 1015 acceptance is owner-reported physical installation and operation, not a blanket guarantee of future builds or full playback/hotpatch acceptance. Keep older release assets for rollback. PlayTorrio is a read-only reference, not a deployment target.
