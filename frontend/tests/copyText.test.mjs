import assert from "node:assert/strict"
import ts from "typescript"
import { readFileSync } from "node:fs"

const source = readFileSync(new URL("../src/utils/copyText.ts", import.meta.url), "utf8")
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText
const { copyWithCommand } = await import(`data:text/javascript,${encodeURIComponent(compiled)}`)

function fakeDocument(result) {
  const events = []
  const area = {
    value: "",
    style: {},
    setAttribute(name, value) { events.push(["attr", name, value]) },
    focus() { events.push(["focus"]) },
    select() { events.push(["select"]) },
    setSelectionRange(start, end) { events.push(["range", start, end]) },
  }
  return {
    events,
    area,
    doc: {
      createElement(tag) {
        events.push(["create", tag])
        return area
      },
      execCommand(command) {
        events.push(["command", command, area.value])
        if (result === "throw") throw new Error("blocked")
        return result
      },
      body: {
        appendChild(node) { events.push(["append", node.value]) },
        removeChild(node) { events.push(["remove", node === area]) },
      },
    },
  }
}

const password = "Ab3d-Ef4g-Hi5j-Km6n"
const success = fakeDocument(true)
assert.equal(copyWithCommand(password, success.doc), true)
assert.deepEqual(success.events, [
  ["create", "textarea"],
  ["attr", "readonly", ""],
  ["append", password],
  ["focus"],
  ["select"],
  ["range", 0, password.length],
  ["command", "copy", password],
  ["remove", true],
])

const failure = fakeDocument(false)
assert.equal(copyWithCommand(password, failure.doc), false)
assert.equal(failure.events.at(-2)[2], password)
assert.equal(failure.events.at(-1)[0], "remove")

const blocked = fakeDocument("throw")
assert.equal(copyWithCommand(password, blocked.doc), false)
assert.equal(blocked.events.at(-1)[0], "remove")

console.log("copyText: 3 passed")
