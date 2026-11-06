# Cross-language release policy

DProvenanceKit Swift and DProvenanceKitPython are language-specific SDKs for one product. Starting with the coordinated 0.9.2 release, both SDKs use the same product release version.

## What a shared version means

- The version identifies the coordinated DProvenanceKit product release.
- Each SDK retains its own package artifact, API surface, dependency requirements, platform support, and release validation.
- A shared version does not promise feature parity, source compatibility across languages, or that a capability available in one SDK exists in the other.
- The capability matrices in the Swift and Python READMEs are authoritative for language-specific support.

## Release coordination

1. Prepare both release candidates against the same product version.
2. Require each repository's full CI and release-specific checks to pass independently.
3. Publish each SDK through its own release process: Swift GitHub Release and Python GitHub Release/PyPI Trusted Publishing.
4. Announce a coordinated release only after both publication paths are confirmed.

If one language's validation or publication fails, report the other SDK's status accurately and do not describe the product release as complete. Never reuse or rewrite an existing release tag or published package version.

## 0.9.2 status

Python PR #93 and Swift PR #142 prepare the respective 0.9.2 candidates. Neither PR publishes a tag or package. The product release remains pending both repositories' validation and publication gates.
