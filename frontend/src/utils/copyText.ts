/** Minimal surface used to copy during a click without depending on the Clipboard API. */
export interface CopyTarget {
  value: string
  style: { position: string; top: string; left: string; opacity: string }
  setAttribute(name: string, value: string): void
  focus(): void
  select(): void
  setSelectionRange(start: number, end: number): void
}

export interface CopyDocument {
  createElement(tag: "textarea"): CopyTarget
  execCommand(command: string): boolean
  body: {
    appendChild(node: CopyTarget): void
    removeChild(node: CopyTarget): void
  }
}

/**
 * Copy while the click is still active. HTTP pages cannot use the async Clipboard API,
 * and awaiting it first also drops the user gesture required by execCommand.
 */
export function copyWithCommand(value: string, doc: CopyDocument = document as unknown as CopyDocument): boolean {
  const area = doc.createElement("textarea")
  area.value = value
  area.setAttribute("readonly", "")
  area.style.position = "fixed"
  area.style.top = "0"
  area.style.left = "0"
  area.style.opacity = "0"
  doc.body.appendChild(area)
  area.focus()
  area.select()
  area.setSelectionRange(0, value.length)
  try {
    return doc.execCommand("copy")
  } catch {
    return false
  } finally {
    doc.body.removeChild(area)
  }
}
