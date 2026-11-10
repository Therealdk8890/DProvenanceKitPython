# External Attestation Signing Providers

DProvenanceKitPython exposes `TraceAttestationSigningKey`, a small protocol that lets an
application sign DPK-BINARY-V1 attestations without exporting its private key into the
DProvenanceKit process.

The protocol requires two operations:

- `public_key_x963_representation`: return the P-256 public key as an uncompressed X9.63 point.
- `signature_der(data)`: sign the supplied bytes with ECDSA P-256 and SHA-256, returning an
  ASN.1 DER-encoded signature.

The existing `SoftwareTraceAttestationKey` implements this protocol. An application can provide
an adapter over a TPM, HSM, PKCS#11 token, or managed key service when that provider supports the
same curve, digest, public-key encoding, and signature encoding.

## Adapter shape

An adapter can keep only an opaque provider key handle and delegate the two protocol operations:

```python
from dprovenancekit import TraceAttestationSigningKey, signed_document

class HSMTraceSigningKey:
    @property
    def public_key_x963_representation(self) -> bytes:
        # Export the public point only; never export the private scalar.
        return self.session.export_public_key_x963(self.key_handle)

    def signature_der(self, data: bytes) -> bytes:
        # Provider must perform ECDSA P-256 with SHA-256 and return DER.
        return self.session.sign_ecdsa_p256_sha256_der(self.key_handle, data)

document = signed_document(trace, HSMTraceSigningKey())
```

The provider session, authentication, key policy, lifecycle, and error handling remain the
application's responsibility. The adapter must not silently substitute another curve, digest,
or signature encoding.

## Trust boundary

This interface is a signing interoperability contract, **not hardware attestation**.

A successfully verified DPK-BINARY-V1 document establishes that the embedded P-256 public key
validates the signature over the canonical trace envelope. It does not establish:

- that the private key was generated or retained in a TPM, HSM, Secure Enclave, or other hardware;
- that the provider or device is genuine, uncompromised, or running approved software;
- that a provider policy, user-presence check, or key-use restriction was enforced.

The verifier continues to report only `embeddedKeyOnly` or `trustedKey`. The latter means the
key ID matched a trust list supplied independently by the verifier; it does not mean the key is
hardware-backed.

If hardware custody matters, validate the provider's attestation or key-origin evidence through
an independent trust path and bind that evidence to the key ID. Keep the evidence and its
verification policy separate from the DPK trace signature unless and until a versioned,
cross-language format for such claims is designed and reviewed.

## Compatibility

No attestation wire fields or canonicalization rules change. Existing software-generated
attestations remain byte-format compatible with the Swift verifier. External providers must
produce the same P-256/SHA-256 DER signature format; a provider using a different algorithm
requires a separately versioned attestation format and verifier support.
