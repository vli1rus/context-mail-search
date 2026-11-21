'use strict';

const element = (id) => document.getElementById(id);
const list = element('items');
const queryInput = element('query');
const layout = element('layout');
const pageSize = 18;
const customMailboxLimit = 7;
const number = new Intl.NumberFormat('ru-RU');
const previewDate = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short', timeZone: 'UTC' });
const longDate = new Intl.DateTimeFormat('ru-RU', { dateStyle: 'long', timeZone: 'UTC' });
const state = { mailbox: '', saved: false, mode: 'smart', query: '', page: 0, open: null, openedRow: null, editedLetter: false, listRun: 0, letterRun: 0,
  meta: null, storage: '', shortlist: new Set() };

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text != null) item.textContent = text;
  return item;
}

function dateLabel(value, format) {
  if (!value) return '';
  const parsed = new Date(value.slice(0, 10));
  return Number.isFinite(+parsed) ? format.format(parsed) : value;
}

function senderOf(item) {
  return item.sender || 'Отправитель не указан';
}

function titleOf(item) {
  if (prefs.subjects[item.doc_id]) return prefs.subjects[item.doc_id];
  return item.subject || (item.preview || item.body || '').trim().split('\n')[0].slice(0, 84) || 'Письмо без темы';
}

function initials(name) {
  const parts = name.match(/[\p{L}\p{N}]+/gu) || ['П'];
  return parts.slice(0, 2).map(word => word[0]).join('').toUpperCase();
}

async function api(route, values = {}) {
  const params = new URLSearchParams(Object.entries(values).filter(([, value]) => value !== '' && value != null));
  const response = await fetch(`/api/${route}?${params}`);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Сервис недоступен.');
  return { body, timing: response.headers.get('Server-Timing') };
}

function refreshSelection() {
  for (const button of list.querySelectorAll('.mail-item')) {
    button.setAttribute('aria-pressed', String(button.dataset.doc === state.open));
  }
  const saved = state.shortlist.has(state.open);
  element('save').setAttribute('aria-pressed', String(saved));
  element('save').textContent = saved ? '★ В избранном' : '☆ В избранное';
  element('saved-total').textContent = state.shortlist.size ? number.format(state.shortlist.size) : '';
}

function closeLetter(returnFocus = false) {
  const lastOpen = state.open;
  state.letterRun++;
  state.open = null;
  state.openedRow = null;
  state.editedLetter = false;
  element('letter').hidden = true;
  layout.classList.remove('reading');
  refreshSelection();
  if (returnFocus) {
    const button = [...list.querySelectorAll('.mail-item')].find(item => item.dataset.doc === lastOpen);
    button?.focus();
  }
}

function returnToList() {
  if (!state.editedLetter) {
    closeLetter(true);
    return;
  }
  const docId = state.open;
  refresh().then(() => [...list.querySelectorAll('.mail-item')].find(item => item.dataset.doc === docId)?.focus());
}

function highlight(container, text, query) {
  const matching = new Set((query.match(/[\p{L}\p{N}]{3,}/gu) || []).map(word => word.toLowerCase().replaceAll('ё', 'е')));
  let from = 0;
  container.replaceChildren();
  for (const token of text.matchAll(/[\p{L}\p{N}]{3,}/gu)) {
    if (!matching.has(token[0].toLowerCase().replaceAll('ё', 'е'))) continue;
    container.append(document.createTextNode(text.slice(from, token.index)), node('mark', '', token[0]));
    from = token.index + token[0].length;
  }
  container.append(document.createTextNode(text.slice(from)));
}

async function openLetter(row, explicit = false) {
  const run = ++state.letterRun;
  try {
    const { body: letter } = await api('letter', { id: row.doc_id });
    if (run !== state.letterRun) return;
    state.open = letter.doc_id;
    state.openedRow = row;
    element('letter').hidden = false;
    element('letter-subject').textContent = titleOf({ ...letter, preview: row.subject ? '' : row.preview });
    element('letter-sender').textContent = senderOf(letter);
    element('letter-box').textContent = letter.sender ? letter.mailbox_id : '';
    element('letter-avatar').textContent = initials(senderOf(letter));
    element('letter-avatar').className = avatarClass(senderOf(letter));
    element('letter-date').textContent = dateLabel(letter.date, longDate) || 'Дата неизвестна';
    element('letter-date').dateTime = letter.date.slice(0, 10);
    element('letter-body').textContent = letter.body || 'В письме нет текста.';
    element('custom-subject').value = prefs.subjects[letter.doc_id] || '';
    element('custom-subject').placeholder = letter.subject || 'Исходная тема';
    fillLabels(element('message-label'), 'Без ярлыка', prefs.assigned[letter.doc_id] || '');
    element('letter-box').textContent = boxTitle(letter.mailbox_id);
    element('evidence').hidden = !row.evidence;
    if (row.evidence) {
      highlight(element('evidence-text'), row.evidence, state.query);
      element('signals').textContent = (row.signals || []).join(' · ');
    }
    layout.classList.add('reading');
    refreshSelection();
    if (explicit) {
      element('back').focus({preventScroll: true});
      window.scrollTo(0, 0);
    }
  } catch (error) {
    if (run === state.letterRun) element('result-detail').textContent = error.message;
  }
}

function avatarClass(name) {
  let hash = 0;
  for (const char of name) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
  return 'avatar tone-' + hash % 5;
}

function render(rows, emptyMessage) {
  list.replaceChildren();
  if (!rows.length) {
    list.append(node('p', 'empty-items', emptyMessage));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of rows) {
    const button = node('button', 'mail-item');
    button.type = 'button';
    button.dataset.doc = item.doc_id;
    button.setAttribute('aria-pressed', String(state.open === item.doc_id));
    const avatar = node('span', avatarClass(senderOf(item)), initials(senderOf(item)));
    avatar.setAttribute('aria-hidden', 'true');
    const content = node('span', 'mail-text');
    content.append(node('span', 'subject', titleOf(item)),
      node('span', 'item-preview', item.evidence || item.preview || ''));
    const label = prefs.labels[prefs.assigned[item.doc_id]];
    if (label) content.prepend(node('span', 'tag color-' + label.color, label.name));
    button.append(avatar, node('span', 'sender', senderOf(item)), content, node('span', 'time', dateLabel(item.date, previewDate)));
    button.addEventListener('click', () => openLetter(item, true));
    fragment.append(button);
  }
  list.append(fragment);
}

function navSelection() {
  const buttons = [element('all-mail'), element('saved-mail'), ...element('mailboxes').children];
  for (const button of buttons) {
    const selected = !state.label && (button.id === 'saved-mail' ? state.saved : !state.saved && (button.dataset.box || '') === state.mailbox);
    button.setAttribute('aria-pressed', String(selected));
    button.classList.toggle('is-selected', selected);
  }
}

async function refresh() {
  const run = ++state.listRun;
  const searching = Boolean(state.query && !state.saved && !state.label);
  const offset = state.page * pageSize;
  closeLetter();
  navSelection();
  element('result-title').textContent = state.label ? prefs.labels[state.label].name : state.saved ? 'Избранное' : searching ? 'Результаты поиска' : boxTitle(state.mailbox) || 'Входящие';
  element('result-detail').textContent = 'Загрузка…';
  element('timing').hidden = true;
  element('modes').hidden = !searching || isCustomMailbox(state.mailbox);
  element('pagination').hidden = true;
  for (const button of element('modes').querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.mode === state.mode));
  list.replaceChildren();
  try {
    const filters = { mailbox: state.mailbox, after: element('after').value, before: element('before').value, as_of: element('as-of').value || state.meta?.reference_day };
    let result;
    let total;
    if (state.saved || state.label) {
      const ids = state.label ? Object.keys(prefs.assigned).filter(id => prefs.assigned[id] === state.label) : [...state.shortlist].reverse();
      const items = await Promise.all(ids.slice(offset, offset + pageSize).map(async (id) => {
        try { const { body } = await api('letter', { id }); return { ...body, preview: body.body.slice(0, 260) }; }
        catch { return null; }
      }));
      result = { body: { items: items.filter(Boolean), total: ids.length } };
    } else if (isCustomMailbox(state.mailbox)) {
      result = { body: { items: [] } };
    } else if (searching) {
      result = await api('search', { ...filters, q: state.query, mode: state.mode, limit: 40 });
    } else {
      result = await api('letters', { ...filters, limit: pageSize, offset });
    }
    if (run !== state.listRun) return;
    const rows = result.body.items;
    total = result.body.total ?? rows.length;
    const timing = result.timing?.match(/dur=([\d.]+)/);
    if (timing) {
      element('timing').textContent = `${number.format(Math.round(10 * Number(timing[1])) / 10)} мс`;
      element('timing').title = 'Время обработки этого запроса на сервере';
      element('timing').hidden = false;
    }
    const ending = new Intl.PluralRules('ru').select(total);
    const noun = {one: 'письмо', few: 'письма', many: 'писем', other: 'письма'}[ending];
    element('result-detail').textContent = searching ? `«${state.query}» · в выдаче ${total}` : `${number.format(total)} ${noun}`;
    render(rows, state.saved ? 'Здесь появятся письма, которые вы добавите в избранное.' :
      searching ? 'Совпадений нет. Попробуйте другое описание или снимите фильтр.' : 'В этом разделе пока нет писем.');
    if (!searching && total > pageSize) {
      element('pagination').hidden = false;
      element('pages').textContent = `${number.format(offset + 1)}–${number.format(Math.min(offset + pageSize, total))} из ${number.format(total)}`;
      element('prev').disabled = state.page === 0;
      element('next').disabled = offset + pageSize >= total;
    }
  } catch (error) {
    if (run === state.listRun) {
      element('result-detail').textContent = 'Не удалось загрузить письма';
      render([], error.message);
    }
  }
}

function selectMailbox(mailbox = '', saved = false, clear = false) {
  state.label = '';
  toggleMenu(false);
  state.mailbox = mailbox;
  state.saved = saved;
  state.page = 0;
  if (clear) { state.query = ''; queryInput.value = ''; }
  refresh();
}

function submitSearch() {
  state.label = '';
  toggleMenu(false);
  state.query = queryInput.value.trim();
  state.saved = false;
  state.page = 0;
  refresh();
}

function resetDates() {
  element('after').value = '';
  element('before').value = '';
  element('as-of').value = state.meta?.reference_day || '';
}

function goToInbox() {
  resetDates();
  if (location.hash.startsWith('#search?')) history.replaceState(null, '', location.pathname + location.search);
  selectMailbox('', false, true);
}

element('home').addEventListener('click', goToInbox);
element('all-mail').addEventListener('click', goToInbox);
element('saved-mail').addEventListener('click', () => selectMailbox('', true, true));
element('search-form').addEventListener('submit', (event) => { event.preventDefault(); submitSearch(); });
queryInput.addEventListener('search', () => { if (!queryInput.value && state.query) submitSearch(); });
element('filters-toggle').addEventListener('click', () => {
  const filters = element('filters');
  filters.hidden = !filters.hidden;
  element('filters-toggle').setAttribute('aria-expanded', String(!filters.hidden));
});
for (const id of ['after', 'before', 'as-of']) element(id).addEventListener('change', () => { state.page = 0; refresh(); });
element('reset-filters').addEventListener('click', () => {
  resetDates();
  state.page = 0;
  refresh();
});
for (const button of element('modes').querySelectorAll('button')) button.addEventListener('click', () => {
  if (state.mode !== button.dataset.mode) { state.mode = button.dataset.mode; refresh(); }
});
element('back').addEventListener('click', returnToList);
element('prev').addEventListener('click', () => { if (state.page > 0) { state.page--; refresh(); } });
element('next').addEventListener('click', () => { state.page++; refresh(); });
element('save').addEventListener('click', () => {
  if (state.open == null) return;
  if (state.shortlist.has(state.open)) state.shortlist.delete(state.open);
  else state.shortlist.add(state.open);
  try { localStorage.setItem(state.storage, JSON.stringify([...state.shortlist])); } catch { /* Selection remains available in this tab. */ }
  refreshSelection();
  if (state.saved) { state.page = 0; refresh(); }
});
function toggleMenu(open) {
  layout.classList.toggle('nav-open', open);
  element('menu').setAttribute('aria-expanded', String(open));
  element('scrim').hidden = !open;
}
element('menu').addEventListener('click', () => toggleMenu(!layout.classList.contains('nav-open')));
element('scrim').addEventListener('click', () => toggleMenu(false));
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    if (layout.classList.contains('nav-open')) { toggleMenu(false); element('menu').focus(); }
    else if (state.open && !document.querySelector('dialog[open]')) returnToList();
  }
  if (event.key === '/' && !document.querySelector('dialog[open]') && !event.altKey && !event.ctrlKey && !event.metaKey && !event.target.matches('input, textarea, [contenteditable]')) {
    event.preventDefault();
    queryInput.focus();
  }
});

function restoreSearchLink(meta) {
  const prefix = '#search?';
  if (!location.hash.startsWith(prefix)) return;
  const link = new URLSearchParams(location.hash.slice(prefix.length));
  const query = (link.get('q') || '').trim();
  const mailbox = link.get('mailbox') || '';
  const asOf = link.get('as_of') || '';
  const mode = link.get('mode') || 'smart';
  if (!query || query.length > queryInput.maxLength ||
      (mailbox && !meta.mailboxes.some(box => box.id === mailbox)) ||
      !['smart', 'lexical', 'semantic'].includes(mode)) return;
  const dateField = element('as-of');
  if (asOf) {
    dateField.value = asOf;
    if (dateField.value !== asOf) {
      resetDates();
      return;
    }
    element('filters').hidden = false;
    element('filters-toggle').setAttribute('aria-expanded', 'true');
  }
  queryInput.value = state.query = query;
  state.mailbox = mailbox;
  state.mode = mode;
}

async function init() {
  try {
    const { body } = await api('meta');
    state.meta = body;
    loadPrefs(body);
    state.storage = `context-shortlist-v1-${body.demo ? 'demo' : 'corpus'}`;
    try {
      const saved = JSON.parse(localStorage.getItem(state.storage) || '[]');
      if (Array.isArray(saved)) state.shortlist = new Set(saved.filter(id => typeof id === 'string'));
    } catch { /* The browser may disable local storage. */ }
    element('mail-total').textContent = number.format(body.count);
    element('source-label').textContent = body.demo ? 'Демонстрационные письма' : 'Архив хакатона';
    element('as-of').value = body.reference_day || '';
    element('semantic-mode').textContent = body.semantic ? 'По смыслу' : 'По теме';
    restoreSearchLink(body);
    renderFolders();
    await refresh();
  } catch (error) {
    element('source-label').textContent = 'Сервис недоступен';
    element('result-detail').textContent = error.message;
  }
}

const colors = {blue:'Синий', green:'Зелёный', purple:'Фиолетовый', orange:'Оранжевый'};
const themes = new Set(['blue', 'sage', 'plum']);
let prefs = {boxes:{},customBoxes:{},labels:{},assigned:{},subjects:{},theme:'blue'};
let prefsKey;
function applyTheme(theme) {
  document.body.dataset.theme = themes.has(theme) ? theme : 'blue';
}
function loadPrefs(meta) {
  prefsKey = 'context-preferences-v1-' + JSON.stringify(meta.mailboxes.map(b => b.id));
  try {
    const saved = JSON.parse(localStorage.getItem(prefsKey));
    if (saved) Object.assign(prefs, saved);
  } catch { /* Defaults work when storage is unavailable. */ }
  if (!themes.has(prefs.theme)) prefs.theme = 'blue';
  applyTheme(prefs.theme);
}
function persist() {
  try { localStorage.setItem(prefsKey, JSON.stringify(prefs)); } catch { /* Available for this session. */ }
}
function isCustomMailbox(id) { return Object.hasOwn(prefs.customBoxes, id); }
function boxTitle(id) { return prefs.boxes[id]?.name || prefs.customBoxes[id]?.name || id; }
function mailboxRows() {
  return [...state.meta.mailboxes, ...Object.keys(prefs.customBoxes).map(id => ({id, count:0}))];
}
function fillLabels(select, empty, selected) {
  select.replaceChildren(new Option(empty, ''));
  for (const [id,label] of Object.entries(prefs.labels)) select.add(new Option(label.name,id));
  select.value = selected;
}
function renderFolders() {
  const boxes = element('mailboxes'), labels = element('labels');
  boxes.replaceChildren(); labels.replaceChildren();
  const add = element('add-box');
  add.hidden = false;
  add.disabled = Object.keys(prefs.customBoxes).length >= customMailboxLimit;
  add.textContent = add.disabled ? 'Добавлено 7 ящиков' : 'Добавить ящик';
  for (const {id,count} of mailboxRows()) {
    const button = node('button', 'nav-row color-' + (prefs.boxes[id]?.color || 'blue'), boxTitle(id));
    button.dataset.box = id; button.title = id;
    button.append(node('span','nav-count',number.format(count)));
    button.onclick = () => selectMailbox(id);
    boxes.append(button);
  }
  for (const [id,label] of Object.entries(prefs.labels)) {
    const button = node('button','nav-row label-link color-' + label.color,label.name);
    button.onclick = () => {
      toggleMenu(false); state.label=id; state.saved=false; state.page=0; state.query=''; queryInput.value=''; state.mailbox=''; refresh();
    };
    labels.append(button);
  }
}
function showBoxSettings() {
  const id = element('edit-box').value;
  element('box-name').value = boxTitle(id);
  element('box-color').value = prefs.boxes[id]?.color || 'blue';
}
function showLabelSettings() {
  const label = prefs.labels[element('edit-label').value];
  element('label-name').value = label?.name || '';
  element('label-color').value = label?.color || 'blue';
}
for (const id of ['box-color','label-color']) {
  for (const [key,name] of Object.entries(colors)) element(id).add(new Option(name,key));
}
element('preferences').onclick = () => {
  if (!state.meta) return;
  toggleMenu(false);
  const select = element('edit-box');
  const mailboxes = mailboxRows();
  select.replaceChildren(...mailboxes.map(b => new Option(boxTitle(b.id),b.id)));
  select.value = state.mailbox || mailboxes[0]?.id || '';
  showBoxSettings(); fillLabels(element('edit-label'),'Новый ярлык',state.label || '');
  showLabelSettings(); element('theme').value = prefs.theme;
  element('settings').showModal();
};
element('edit-box').onchange = showBoxSettings;
element('edit-label').onchange = showLabelSettings;
element('theme').onchange = event => applyTheme(event.target.value);
element('settings').addEventListener('close', () => applyTheme(prefs.theme));
element('cancel-settings').onclick = () => element('settings').close();
element('add-box').onclick = () => {
  const input = element('new-box-name');
  input.value = '';
  input.setCustomValidity('');
  element('new-box-hint').textContent = 'Ящик создаётся пустым и сохраняется в этом браузере.';
  element('new-box').showModal();
};
element('cancel-new-box').onclick = () => element('new-box').close();
element('new-box-name').oninput = event => event.target.setCustomValidity('');
element('new-box-form').onsubmit = event => {
  event.preventDefault();
  if (Object.keys(prefs.customBoxes).length >= customMailboxLimit) return;
  const input = element('new-box-name');
  const name = input.value.trim();
  if (!name) {
    input.setCustomValidity('Введите название ящика.');
    input.reportValidity();
    return;
  }
  let id = 'local-1';
  for (let index = 2; mailboxRows().some(box => box.id === id); index++) id = 'local-' + index;
  prefs.customBoxes[id] = {name};
  persist();
  renderFolders();
  element('new-box').close();
  selectMailbox(id, false, true);
};
element('settings-form').onsubmit = event => {
  event.preventDefault();
  const id=element('edit-box').value;
  if (id) prefs.boxes[id]={name:element('box-name').value.trim() || prefs.customBoxes[id]?.name || id,color:element('box-color').value};
  const name=element('label-name').value.trim();
  if (name) {
    const key=element('edit-label').value || 'label-' + Date.now().toString(36);
    prefs.labels[key]={name,color:element('label-color').value};
  }
  prefs.theme=element('theme').value;
  applyTheme(prefs.theme);
  persist(); renderFolders(); element('settings').close(); refresh();
};
element('message-label').onchange = event => {
  if (event.target.value) prefs.assigned[state.open]=event.target.value;
  else delete prefs.assigned[state.open];
  persist();
  state.editedLetter = true;
};
element('apply-subject').onclick = () => {
  const text=element('custom-subject').value.trim();
  if (text) prefs.subjects[state.open]=text; else delete prefs.subjects[state.open];
  persist();
  state.editedLetter = true;
  if (text) element('letter-subject').textContent = text;
  else if (state.openedRow) openLetter(state.openedRow);
};
init();
