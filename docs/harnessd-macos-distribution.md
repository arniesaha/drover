# macOS harnessd distribution

`Harnessd macOS notarization` builds workspace package and command
`drover-harnessd` (`harnessd-rs/`) for `aarch64-apple-darwin`, signs it as
`fyi.drover.harnessd` with Developer ID Application identity `DK2PC4RH5G`, and
submits a ZIP of that signed command to Apple's notary service. The command has
no entitlements: it is a standalone daemon and does not need an entitlement to
perform its supported work.

The callable workflow is invoked from `release.yml` only after the GitHub
release exists, then uploads the signed binary and its SHA-256 companion. A
manual dispatch takes a ref and never publishes an asset; it is the required
way to rehearse signing and notarisation before a release.

The Developer ID `.p12` is imported only into a temporary, unlocked keychain.
The API-key `.p8` is decoded with mode `0600`, and both it and the keychain are
removed in an `always()` cleanup step. The credentials are supplied by the
`macos-signing` environment. This environment is intentionally separate from
the reviewer-gated `ios-testflight-upload` environment: the former permits
unattended harnessd signing and notarization, while the latter remains the
approval boundary for TestFlight uploads. Configure the macOS environment with
the Developer ID and App Store Connect secrets required by this workflow; do
not move or relax the TestFlight environment's protections.

Apple does not staple tickets to a bare Mach-O binary, and `spctl --type
execute` assesses app bundles rather than standalone commands. Consequently the
workflow verifies the notarised binary with `spctl -a -vv -t open --context
context:primary-signature`; Gatekeeper uses its normal online notarisation
lookup when the downloaded command is run.
