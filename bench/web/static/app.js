// Tiny helpers shared by the Northwind Ops pages.
const TASK = document.body.dataset.task;

async function api(kind, body) {
  const opts = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const r = await fetch(`/api/${TASK}/${kind}`, opts);
  return r.json();
}

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k === 'value') el.value = v;
    else if (k === 'checked') el.checked = !!v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
}

let _uid = 0;
const uid = (p) => `${p}-${++_uid}`;

function field(label, input) {
  if (!input.id) input.id = uid('f');
  return h('div', { class: 'field' }, h('label', { for: input.id }, label), input);
}

function select(options, value, attrs = {}) {
  const el = h('select', attrs, options.map((o) => {
    const [v, l] = Array.isArray(o) ? o : [o, o];
    return h('option', { value: v }, l);
  }));
  el.value = value;
  return el;
}

function notice(msg, kind = 'ok') {
  const n = document.getElementById('notice');
  n.textContent = msg;
  n.className = 'notice ' + kind;
  n.hidden = false;
}

const money = (n) => '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
