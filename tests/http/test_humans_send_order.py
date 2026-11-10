"""Distinct signed sends must reach the nonce guard in the order they were submitted.

Execute the real composer with a delayed signature or outbound request, then feed its
wire payloads to the app. The browser probe also drives real clicks and WebCrypto.
"""

import json
import shutil
import subprocess

import _client
import pytest

client = _client.client
NODE = shutil.which("node")

PROBE = r"""
const assert = require('node:assert/strict');
const { createPrivateKey, sign } = require('node:crypto');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const source = input.html.match(/  function send\(\) \{[\s\S]*?\n  \}/)[0];
const key = createPrivateKey({
  key: Buffer.from('302e020100300506032b657004220420' +
    '000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f', 'hex'),
  format: 'der', type: 'pkcs8',
});
const wire = [], errors = [];
let release, nonce = 100, signed = 0;
const gate = new Promise(resolve => { release = resolve; });
const tick = () => new Promise(setImmediate);
const env = {
  Promise, String, TextEncoder, encodeURIComponent, signedSends: {},
  me: { did: 'did:key:z6MkehRgf7yJbgaGfYsdoAsKdBPE3dj2CYhowQdcjqSJgvVd', key },
  room: 'send-order', draftRevision: 0,
  textEl: { value: 'first distinct message', focus() {} }, nickEl: { value: 'guest' },
  swept: value => value, nextNonce: () => ++nonce,
  bytesB64u: value => Buffer.from(value).toString('base64url'),
  crypto: { subtle: { sign(algorithm, key, bytes) {
    const signature = sign(null, bytes, key);
    return ++signed === 1 && input.delay === 'signature'
      ? gate.then(() => signature) : Promise.resolve(signature);
  } } },
  fetch: async (url, options) => {
    const payload = JSON.parse(options.body);
    if (payload.text === 'first distinct message' && input.delay !== 'signature') {
      await gate;
      if (input.delay === 'failure') throw new Error('connection closed before write');
    }
    wire.push({ url, payload });
    return { ok: true };
  },
  fail: error => errors.push(error), stopPoll() {}, pump() {},
};
vm.createContext(env);
vm.runInContext(source, env);
env.send();
(async () => {
  await tick();
  env.textEl.value = 'second distinct message';
  env.draftRevision++;
  env.send();
  await tick();
  release();
  await tick();
  // A failed predecessor must not poison the lane for a later send.
  env.textEl.value = 'third distinct message';
  env.draftRevision++;
  env.send();
  await tick();
  assert.deepEqual(errors, input.delay === 'failure' ? ['connection closed before write'] : []);
  assert.equal(env.textEl.value, '');
  console.log(JSON.stringify(wire));
})();
"""


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the served composer")
@pytest.mark.parametrize("delay", ["request", "signature", "failure"])
def test_distinct_signed_sends_keep_nonce_order(client, delay):
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", PROBE],
        input=json.dumps({"html": client.get("/humans").text, "delay": delay}),
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    requests = json.loads(result.stdout)
    responses = [client.post(r["url"], json=r["payload"]) for r in requests]
    assert all(r.status_code == 200 for r in responses), [
        (r.status_code, r.text.splitlines()[0]) for r in responses
    ]
    expected = ["first distinct message", "second distinct message", "third distinct message"]
    if delay == "failure":
        expected.pop(0)
    view = client.get("/r/send-order?format=json").json()
    assert [message["text"] for message in view["messages"]] == expected
