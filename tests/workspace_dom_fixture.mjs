// Small DOM with move/removal focus semantics for incremental-render tests.
export function createDocument() {
  const document = {activeElement:null};
  class Node {
    constructor(tag) {
      this.tag = tag; this.dataset = {}; this.childNodes = []; this.parentNode = null;
      this.attributes = {}; this.events = {}; this.open = false; this.hidden = false;
      this.className = ""; this._text = "";
      this.classList = {add: value => {this.className += ` ${value}`;}};
    }
    get children() { return this.childNodes; }
    get childElementCount() { return this.childNodes.length; }
    get textContent() { return this._text + this.childNodes.map(n => n.textContent).join(""); }
    set textContent(value) { this.replaceChildren(); this._text = String(value); }
    contains(node) { return this === node || this.childNodes.some(child => child.contains(node)); }
    insertBefore(node, before) {
      node.remove();
      const index = before ? this.childNodes.indexOf(before) : this.childNodes.length;
      if (index < 0) throw new Error("reference node is not a child");
      this.childNodes.splice(index, 0, node); node.parentNode = this;
    }
    append(...nodes) { for (const node of nodes) this.insertBefore(node, null); }
    remove() {
      if (!this.parentNode) return;
      if (this.contains(document.activeElement)) document.activeElement = null;
      this.parentNode.childNodes.splice(this.parentNode.childNodes.indexOf(this), 1);
      this.parentNode = null;
    }
    replaceChildren(...nodes) { for (const node of [...this.childNodes]) node.remove(); this._text = ""; this.append(...nodes); }
    setAttribute(name, value) { this.attributes[name] = value; }
    removeAttribute(name) { delete this.attributes[name]; if (name === "href") delete this.href; }
    addEventListener(name, fn) { this.events[name] = fn; }
    focus() { document.activeElement = this; }
  }
  document.createElement = tag => new Node(tag);
  return document;
}
export function descendants(node) { return [node, ...node.childNodes.flatMap(descendants)]; }
