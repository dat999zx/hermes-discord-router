// Runs the shipped reconciler, not a reimplementation. Network and stores are
// controlled so the sole changing input is the external persisted transcript.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
const [bundle, expected] = process.argv.slice(2)
const text = fs.readFileSync(bundle, 'utf8')
const start = text.indexOf('async function io({requestSequenceRef:')
assert(start >= 0, 'known shipped tile reconciler not found')
const end = text.indexOf('async function ao(', start)
assert(end > start, 'known shipped reconciler end not found')
const tiles = [{ storedSessionId: 'stored-foreign', runtimeId: 'tile-foreign' }]
let persisted = 'old'
let reads = 0
let state = { messages: [{ role: 'assistant', content: 'old' }] }
const identity = value => value
const context = {
  Pt: { get: () => tiles }, to: tile => `tile:${tile.storedSessionId}`,
  no: id => `tile-meta:${id}`, I: () => false, st: { get: () => 'primary-runtime' },
  Za: () => undefined, ro: () => ({ count: 1 }), Ha: () => 'stale-sidebar-row',
  le: () => null, D: { get: () => ({ 'tile-foreign': state }) }, pi: () => false,
  m: async () => { reads++; return { messages: [{ role: 'assistant', content: persisted }] } },
  $a: () => false, eo: () => false, Ua: messages => JSON.stringify(messages),
  ci: async messages => messages, Xt: identity, si: () => async () => [],
  nn: identity, tn: identity, sa: identity, ai: identity
}
vm.createContext(context)
vm.runInContext(text.slice(start, end) + ';globalThis.reconcile = io', context)
const args = { requestSequenceRef: { current: 0 }, signatureRef: { current: new Map() }, updateSessionState: (_id, update) => { state = update(state) } }
await context.reconcile(args)
assert.equal(reads, 1)
persisted = 'new foreign answer'
await context.reconcile(args)
if (expected === 'stale') {
  assert.equal(reads, 1)
  assert.equal(state.messages.at(-1).content, 'old')
} else {
  assert.equal(reads, 2)
  assert.equal(state.messages.at(-1).content, 'new foreign answer')
  await context.reconcile(args)
  assert.equal(reads, 3, 'metadata cannot suppress a read')
}
console.log(`PASS actual packaged reconciler: ${expected}; tail reads=${reads}; tail=${state.messages.at(-1).content}`)
