import json
from dataclasses import replace
from pathlib import Path

import pytest

from experimental.event_chain.core import (
    ChainError, Event, RunClose, append, build, close_preimage, entry_preimage,
    event_bytes, genesis_preimage, verify, verify_close_binding,
)

FIXTURE = Path(__file__).resolve().parents[1] / 'conformance/vectors/event_chain_v1_candidate.json'
CASES = json.loads(FIXTURE.read_text(encoding='utf-8'))['positiveCases']
BASE = CASES[2]
RUN = BASE['runID']
EVENTS = tuple(Event(**entry['event']) for entry in BASE['entries'])


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['name'])
def test_frozen_candidate_bytes(case):
    events = [Event(**entry['event']) for entry in case['entries']]
    chain = build(case['runID'], events)
    verify(chain, case['runID'], expected_count=case['entryCount'],
           expected_head=bytes.fromhex(case['head']))
    assert genesis_preimage(case['runID']).hex() == case['genesisPreimageHex']
    assert chain.head.hex() == case['head']
    assert len(chain.entries) == case['entryCount']
    incremental = build(case['runID'], [])
    for actual, expected in zip(chain.entries, case['entries']):
        assert actual.chainIndex == expected['chainIndex']
        assert actual.previousHash.hex() == expected['previousHash']
        assert actual.entryHash.hex() == expected['entryHash']
        assert entry_preimage(actual.chainIndex, actual.previousHash, actual.event).hex() == expected['preimageHex']
        incremental = append(incremental, actual.event, case['runID'])
    assert incremental == chain


@pytest.mark.parametrize('changes,code', [
    ({'eventSequence': 4}, 'SEQUENCE_NONMONOTONIC'),
    ({'eventSequence': 2}, 'SEQUENCE_NONMONOTONIC'),
    ({'eventID': EVENTS[0].eventID.upper()}, 'DUPLICATE_EVENT_ID'),
    ({'runID': CASES[3]['runID']}, 'RUN_ID_MISMATCH'),
    ({'eventID': 'x'*129}, 'IDENTIFIER_TOO_LONG'),
    ({'eventID': 'bad'}, 'INVALID_IDENTIFIER'),
    ({'eventSequence': 2**64}, 'INTEGER_OUT_OF_RANGE'),
    ({'eventSequence': True}, 'INVALID_INTEGER'),
    ({'eventSequence': 6.0}, 'INVALID_INTEGER'),
    ({'payloadJSON': '{"a":1,"a":2}'}, 'INVALID_PAYLOAD_JSON'),
    ({'payloadJSON': '{"a":NaN}'}, 'INVALID_PAYLOAD_JSON'),
    ({'payloadJSON': '[]'}, 'INVALID_PAYLOAD_JSON'),
    ({'payloadJSON': '{'}, 'INVALID_PAYLOAD_JSON'),
    ({'contextID': '\ud800'}, 'INVALID_STRING'),
    ({'timestampUnixMicroseconds': 2**63}, 'INTEGER_OUT_OF_RANGE'),
])
def test_invalid_append_is_nonmutating(changes, code):
    chain = build(RUN, EVENTS)
    fields = {'eventID': '00000000-0000-4000-8000-000000000099', 'eventSequence': 6}
    fields.update(changes)
    event = replace(EVENTS[-1], **fields)
    with pytest.raises(ChainError, match=f'^{code}$'):
        append(chain, event, RUN)
    assert chain == build(RUN, EVENTS)


@pytest.mark.parametrize('change,code', [
    ('index', 'CHAIN_INDEX_MISMATCH'), ('previous', 'PREVIOUS_HASH_MISMATCH'),
    ('payload', 'ENTRY_HASH_MISMATCH'), ('metadata', 'ENTRY_HASH_MISMATCH'),
    ('delete_middle', 'CHAIN_INDEX_MISMATCH'), ('reorder', 'CHAIN_INDEX_MISMATCH'),
    ('head', 'HEAD_MISMATCH'), ('version', 'UNSUPPORTED_VERSION'),
    ('bad_hash', 'INVALID_HASH'), ('wrong_run', 'RUN_ID_MISMATCH'),
])
def test_tampering(change, code):
    chain = build(RUN, EVENTS)
    a, b, c = chain.entries
    if change == 'index':
        chain = replace(chain, entries=(a, replace(b, chainIndex=4), c))
    elif change == 'previous':
        chain = replace(chain, entries=(a, replace(b, previousHash=b'\xff'*32), c))
    elif change == 'payload':
        chain = replace(chain, entries=(a, replace(b, event=replace(b.event, payloadJSON='{}')), c))
    elif change == 'metadata':
        chain = replace(chain, entries=(a, replace(b, event=replace(b.event, engineName='other')), c))
    elif change == 'delete_middle':
        chain = replace(chain, entries=(a, c))
    elif change == 'reorder':
        chain = replace(chain, entries=(b, a, c))
    elif change == 'head':
        chain = replace(chain, head=b'\xff'*32)
    elif change == 'version':
        chain = replace(chain, version=2)
    elif change == 'bad_hash':
        chain = replace(chain, entries=(a, replace(b, entryHash=b'\xff'), c))
    else:
        chain = replace(chain, runID=CASES[3]['runID'])
    with pytest.raises(ChainError, match=f'^{code}$'):
        verify(chain, RUN)


def test_truncated_tail_requires_trusted_commitment():
    chain = build(RUN, EVENTS)
    truncated = build(RUN, EVENTS[:-1])
    verify(truncated, RUN)
    with pytest.raises(ChainError, match='^CLOSURE_MISMATCH$'):
        verify(truncated, RUN, expected_count=len(chain.entries), expected_head=chain.head)


def test_recomputed_replacement_requires_trusted_commitment():
    chain = build(RUN, EVENTS)
    replacement = build(RUN, (replace(EVENTS[0], payloadJSON='{}'), *EVENTS[1:]))
    verify(replacement, RUN)
    with pytest.raises(ChainError, match='^CLOSURE_MISMATCH$'):
        verify(replacement, RUN, expected_count=len(chain.entries), expected_head=chain.head)


def test_empty_evidence_policy():
    with pytest.raises(ChainError, match='^EMPTY_CHAIN$'):
        verify(build(RUN, []), RUN, require_evidence=True)


def test_bad_existing_chain_cannot_append():
    chain = replace(build(RUN, EVENTS), head=b'\x00'*32)
    with pytest.raises(ChainError, match='^HEAD_MISMATCH$'):
        append(chain, EVENTS[-1], RUN)


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['name'])
def test_incremental_builder_matches_golden(case):
    from experimental.event_chain.core import ChainBuilder

    writer = ChainBuilder(case['runID'])
    for item in case['entries']:
        entry = writer.append(Event(**item['event']))
        assert entry.entryHash.hex() == item['entryHash']
        assert entry.previousHash.hex() == item['previousHash']
    assert writer.head.hex() == case['head']
    assert writer.count == case['entryCount']
    verify(writer.snapshot(), case['runID'], expected_head=bytes.fromhex(case['head']))


def test_builder_resume_and_snapshot_isolation():
    from experimental.event_chain.core import ChainBuilder

    prefix = build(RUN, EVENTS[:2])
    writer = ChainBuilder.from_chain(prefix, RUN, expected_count=2, expected_head=prefix.head)
    old = writer.snapshot()
    writer.append(EVENTS[2])
    assert old == prefix
    assert writer.snapshot() == build(RUN, EVENTS)
    assert old.head != writer.head


@pytest.mark.parametrize('changes,code', [
    ({'eventSequence': 2}, 'SEQUENCE_NONMONOTONIC'),
    ({'eventID': EVENTS[0].eventID}, 'DUPLICATE_EVENT_ID'),
    ({'runID': CASES[3]['runID']}, 'RUN_ID_MISMATCH'),
    ({'payloadJSON': '{'}, 'INVALID_PAYLOAD_JSON'),
    ({'timestampUnixMicroseconds': 2**63}, 'INTEGER_OUT_OF_RANGE'),
])
def test_builder_rejected_append_does_not_poison_state(changes, code):
    from experimental.event_chain.core import ChainBuilder

    writer = ChainBuilder.from_chain(build(RUN, EVENTS[:2]), RUN)
    before = writer.snapshot()
    with pytest.raises(ChainError, match=f'^{code}$'):
        writer.append(replace(EVENTS[2], **changes))
    assert writer.snapshot() == before
    writer.append(EVENTS[2])
    assert writer.snapshot() == build(RUN, EVENTS)


def test_builder_rejects_corrupted_import_and_untrusted_commitment():
    from experimental.event_chain.core import ChainBuilder

    chain = build(RUN, EVENTS)
    with pytest.raises(ChainError, match='^HEAD_MISMATCH$'):
        ChainBuilder.from_chain(replace(chain, head=b'\x00'*32), RUN)
    with pytest.raises(ChainError, match='^CLOSURE_MISMATCH$'):
        ChainBuilder.from_chain(chain, RUN, expected_count=4)
    with pytest.raises(ChainError, match='^CLOSURE_MISMATCH$'):
        ChainBuilder.from_chain(chain, RUN, expected_head=b'\xff'*32)


def test_builder_hashes_only_new_entries(monkeypatch):
    from uuid import UUID

    from experimental.event_chain import core

    calls = 0
    original = core.sha256

    def count_hash(data):
        nonlocal calls
        calls += 1
        return original(data)

    monkeypatch.setattr(core, 'sha256', count_hash)
    writer = core.ChainBuilder(RUN)
    for i in range(1024):
        writer.append(replace(EVENTS[0], eventID=str(UUID(int=i+1)), eventSequence=i))
    assert calls == 1025  # genesis plus one digest per new entry
    assert writer.count == 1024
    snapshot = writer.snapshot()
    assert calls == 1025  # snapshots don't rehash
    verify(snapshot, RUN)
    assert calls == 2050  # separate, full verification


def test_builder_resume_hashes_prefix_once(monkeypatch):
    from experimental.event_chain import core

    prefix = build(RUN, EVENTS[:2])
    calls = 0
    original = core.entry_preimage

    def count_preimage(index, previous, event):
        nonlocal calls
        calls += 1
        return original(index, previous, event)

    monkeypatch.setattr(core, 'entry_preimage', count_preimage)
    writer = core.ChainBuilder.from_chain(prefix, RUN)
    assert calls == 2
    writer.append(EVENTS[2])
    assert calls == 3


@pytest.mark.parametrize("extra,accepted", [(256, True), (257, False)])
def test_payload_depth_boundary(extra, accepted):
    payload = '{"x":' + '[' * extra + '0' + ']' * extra + '}'
    event = replace(EVENTS[0], payloadJSON=payload)
    if accepted:
        event_bytes(event)
    else:
        with pytest.raises(ChainError, match='^INVALID_PAYLOAD_JSON$'):
            event_bytes(event)


@pytest.mark.parametrize("extra,accepted", [(0, True), (1, False)])
def test_payload_utf8_byte_limit(extra, accepted):
    payload = '{"x":"' + 'é' * ((1_048_576 - 8) // 2) + 'a' * extra + '"}'
    assert len(payload.encode('utf-8')) == 1_048_576 + extra
    event = replace(EVENTS[0], payloadJSON=payload)
    if accepted:
        event_bytes(event)
    else:
        with pytest.raises(ChainError, match='^INVALID_PAYLOAD_JSON$'):
            event_bytes(event)


@pytest.mark.parametrize("payload", [
    '{"é":1,"e\\u0301":2}', '{"x":"[[[[[[\\""}', '{"x":1e999999}',
    '{"\\uD800":0}', '{"x":[true,null,-1.2e+5]}',
])
def test_valid_payload_grammar(payload):
    event_bytes(replace(EVENTS[0], payloadJSON=payload))


@pytest.mark.parametrize("payload", [
    '{"a":1,"\\u0061":2}', '{"😀":1,"\\uD83D\\uDE00":2}', '{"x":{"a":1,"a":2}}',
])
def test_duplicate_decoded_keys(payload):
    with pytest.raises(ChainError, match='^INVALID_PAYLOAD_JSON$'):
        event_bytes(replace(EVENTS[0], payloadJSON=payload))


CLOSE_CASES = json.loads(FIXTURE.read_text(encoding='utf-8'))['unsignedClosureCases']


def close_from_fixture(case):
    return RunClose(
        case['runID'], case['chainVersion'], case['finalChainIndex'],
        bytes.fromhex(case['finalChainHead']), case['eventCount'],
        bytes.fromhex(case['traceDigest']), bytes.fromhex(case['keyID']),
        case['issuedAtUnixMicroseconds'],
    )


@pytest.mark.parametrize('case', CLOSE_CASES, ids=lambda case: case['name'])
def test_unsigned_close_golden(case):
    from hashlib import sha256

    close = close_from_fixture(case)
    raw = close_preimage(close)
    assert raw.hex() == case['preimageHex']
    assert sha256(raw).hexdigest() == case['preimageSHA256']
    events = next(c for c in CASES if c['name'] == case['name'])['entries']
    chain = build(case['runID'], [Event(**entry['event']) for entry in events])
    verify_close_binding(chain, close, case['runID'])


def test_unsigned_close_rejects_truncation_and_replacement():
    chain = build(RUN, EVENTS)
    close = close_from_fixture(CLOSE_CASES[1])
    verify_close_binding(chain, close, RUN)
    for changed in (build(RUN, EVENTS[:-1]),
                    build(RUN, (replace(EVENTS[0], payloadJSON='{}'), *EVENTS[1:]))):
        with pytest.raises(ChainError, match='^CLOSURE_MISMATCH$'):
            verify_close_binding(changed, close, RUN)


@pytest.mark.parametrize('changes,code', [
    ({'finalChainIndex': 0}, 'CLOSURE_MISMATCH'),
    ({'finalChainHead': b'\x00' * 32}, 'CLOSURE_MISMATCH'),
    ({'traceDigest': b''}, 'INVALID_HASH'),
    ({'keyID': b''}, 'INVALID_HASH'),
    ({'chainVersion': 2}, 'UNSUPPORTED_VERSION'),
    ({'eventCount': -1}, 'INTEGER_OUT_OF_RANGE'),
])
def test_invalid_empty_close(changes, code):
    with pytest.raises(ChainError, match=f'^{code}$'):
        close_preimage(replace(close_from_fixture(CLOSE_CASES[0]), **changes))
