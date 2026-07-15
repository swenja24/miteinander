const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const formatDate = value => value ? new Intl.DateTimeFormat('de-DE', {day:'2-digit',month:'short',year:'numeric'}).format(new Date(value + (value.length === 10 ? 'T12:00:00' : ''))) : 'Ohne Frist';
const money = value => new Intl.NumberFormat('de-DE',{style:'currency',currency:'EUR'}).format(Number(value)||0);
const today = () => new Date().toISOString().slice(0,10);
let state = null;
let ledgerAccounts = new Set();
const taskFilters = {category:'all',assignee:'all',due:'all',status:'all'};

async function request(path, options = {}) {
  const response = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || 'Das hat nicht geklappt.');
  return result;
}
async function refresh() { state = await request('/api/data'); render(); }
function toast(message) { const node=$('#toast'); node.textContent=message; node.classList.add('show'); setTimeout(()=>node.classList.remove('show'),2200); }
function page() { return location.hash.slice(1) || 'dashboard'; }
function memberName(id) { return state.members.find(m=>m.id===id)?.name || 'Nicht zugewiesen'; }
function accountName(id) { return state.accounts?.find(a=>a.id===id)?.name || 'Ohne Kontozuordnung'; }
function userName(id,fallback='Unbekannt') { return state.users?.find(u=>u.id===id)?.displayName || (id===state.currentUser?.id?state.currentUser.displayName:fallback); }
function statusLabel(status) { return ({draft:'Entwurf',collecting:'Unterlagen sammeln',submitted:'Eingereicht',question:'Rückfrage',approved:'Bewilligt',done:'Erledigt',open:'Offen',planned:'Geplant',refused:'Linea wollte nicht'}[status] || status || 'Offen'); }
function statusClass(status) { return ['submitted','approved','done'].includes(status) ? '' : ['question','collecting','refused'].includes(status) ? 'orange' : 'gray'; }

$('#show-password').onclick=()=>{const input=$('#password');input.type=input.type==='password'?'text':'password';};
$('#login-form').onsubmit=async event=>{event.preventDefault();try{await request('/api/login',{method:'POST',body:JSON.stringify({username:$('#username').value,password:$('#password').value})});await start();}catch(error){$('#login-error').textContent=error.message;}};
$('#logout').onclick=async()=>{await request('/api/logout',{method:'POST'});location.reload();};
$('#menu').onclick=()=>$('.sidebar').classList.toggle('open');
$('.modal-close').onclick=()=>$('#modal').close();
window.addEventListener('hashchange',()=>{render();$('.sidebar').classList.remove('open');});

async function start(){
  try{state=await request('/api/data');$('#login').classList.add('hidden');$('#app').classList.remove('hidden');render();}
  catch{$('#login').classList.remove('hidden');$('#app').classList.add('hidden');}
}

function header(kicker,title,description,action='') { return `<div class="page-head"><div><p class="eyebrow">${kicker}</p><h1>${title}</h1><p>${description}</p></div>${action}</div>`; }
function empty(text){return `<div class="empty">${esc(text)}</div>`;}
function dateBox(date){if(!date)return `<div class="date-box">—<small>offen</small></div>`;const d=new Date(date+'T12:00:00');return `<div class="date-box">${d.getDate()}<small>${d.toLocaleDateString('de-DE',{month:'short'})}</small></div>`;}

function dashboard(){
  const caps=state.capabilities;
  const due=state.tasks.filter(t=>!t.deletedAt&&!['done','refused'].includes(t.status)&&t.due).sort((a,b)=>a.due.localeCompare(b.due));
  const openCases=state.cases.filter(c=>!['approved','done'].includes(c.status));
  const caseDeadlines=(state.correspondence||[]).filter(entry=>entry.eventType==='deadline'&&['open','overdue'].includes(deadlineStatus(entry)));
  const balance=state.ledger.reduce((sum,x)=>sum+(x.type==='income'?1:-1)*Number(x.amount||0),0);
  return `${header(new Date().toLocaleDateString('de-DE',{weekday:'long',day:'numeric',month:'long'}),`Guten Tag, ${esc(state.currentUser?.displayName||state.family.person||'zusammen')}.`,'Hier siehst du, was gerade wichtig ist.')}
  <section class="hero"><div class="welcome"><p class="eyebrow">Gemeinsam den Überblick behalten</p><h2>Was steht als Nächstes an?</h2><p>${caps.tasks?(due.length?`„${esc(due[0].title)}“ ist die nächste anstehende Aufgabe.`:'Aktuell ist keine Aufgabe mit Frist offen. Zeit zum Durchatmen.'):'Du siehst hier nur die Bereiche, die für deinen Zugang freigegeben sind.'}</p>${caps.tasks?'<button class="primary" data-add="task">Aufgabe hinzufügen</button>':''}</div>${caps.tasks?`<div class="stat-card"><div><p class="eyebrow">Offene Fristen</p><div class="big">${due.length}</div><p>${due.filter(x=>x.due<today()).length} davon überfällig</p></div><a href="#tasks">Alle Aufgaben ansehen →</a></div>`:''}</section>
  <section class="stats">${caps.cases?`<div class="card mini-stat"><span class="bubble green">◇</span><div><strong>${openCases.length}</strong><small>laufende Anträge · ${caseDeadlines.length} offene Fristen${caseDeadlines.filter(entry=>deadlineStatus(entry)==='overdue').length?` · ${caseDeadlines.filter(entry=>deadlineStatus(entry)==='overdue').length} überschritten`:''}</small></div></div>`:''}${caps.documents?`<div class="card mini-stat"><span class="bubble purple">▤</span><div><strong>${state.documents.length}</strong><small>Dokumente erfasst</small></div></div>`:''}${caps.ledger?`<div class="card mini-stat"><span class="bubble orange">€</span><div><strong>${money(balance)}</strong><small>aktueller Kassenstand</small></div></div>`:''}</section>
  <section class="grid-2">${caps.tasks?`<div class="card"><div class="card-head"><h2>Nächste Aufgaben</h2><a class="link-btn" href="#tasks">Alle ansehen</a></div><div class="item-list">${due.slice(0,4).map(t=>`<div class="list-item">${dateBox(t.due)}<div class="grow"><strong>${esc(t.title)}</strong><small>${esc(memberName(t.assignee))}</small></div><span class="status ${t.due<today()?'orange':''}">${t.due<today()?'Überfällig':'Offen'}</span></div>`).join('')||empty('Noch keine Aufgaben vorhanden.')}</div></div>`:''}
  ${caps.cases?`<div class="card"><div class="card-head"><h2>Laufende Anträge</h2><a class="link-btn" href="#cases">Alle ansehen</a></div><div class="item-list">${state.cases.slice(0,4).map(c=>`<div class="list-item"><span class="bubble green">◇</span><div class="grow"><strong>${esc(c.title)}</strong><small>${esc(c.authority||'Keine Behörde angegeben')}</small></div><span class="status ${statusClass(c.status)}">${esc(statusLabel(c.status))}</span></div>`).join('')||empty('Lege den ersten Antrag an.')}</div></div>`:''}</section>`;
}

function attachmentLink(item){return item.attachmentFile?`<a class="case-file" href="/api/case-files/${encodeURIComponent(item.attachmentFile)}" target="_blank">▤ ${esc(item.attachmentName||'Dokument öffnen')}</a>`:'';}
function deadlineStatus(entry){if(entry.deadlineStatus&&entry.deadlineStatus!=='open')return entry.deadlineStatus;return entry.date&&entry.date<today()?'overdue':'open';}
function casesPage(){
  const topLevel=state.cases.filter(c=>!c.parentCaseId);
  const caseCard=c=>{const children=state.cases.filter(child=>child.parentCaseId===c.id);const events=state.correspondence.filter(entry=>entry.caseId===c.id).sort((a,b)=>(b.date||b.createdAt).localeCompare(a.date||a.createdAt));const deadlines=events.filter(entry=>entry.eventType==='deadline'&&['open','overdue'].includes(deadlineStatus(entry))).sort((a,b)=>(a.date||'9999').localeCompare(b.date||'9999'));const nextDeadline=deadlines[0];return `<article class="card case-card"><div class="case-summary"><div><p class="eyebrow">${esc(c.area||'Allgemeiner Antrag')} · ${c.parentCaseId?'Ergänzungsantrag':'Antrag'}</p><h2>${esc(c.title)}</h2><p>${esc(c.description||'Keine Notiz')}</p>${attachmentLink(c)}</div><div><strong>${esc(c.authority||'Keine Behörde angegeben')}</strong><p>${esc(memberName(c.assignee))} · verantwortlich</p><p>${c.submittedAt?'Versendet: '+formatDate(c.submittedAt):'Noch nicht als versendet erfasst'}</p><p>${c.receivedAt?'Eingegangen: '+formatDate(c.receivedAt):''}</p></div><div><span class="status ${statusClass(c.status)}">${esc(statusLabel(c.status))}</span>${nextDeadline?`<div class="next-deadline ${deadlineStatus(nextDeadline)}"><small>Nächste Frist</small><strong>${formatDate(nextDeadline.date)}</strong><span>${esc(nextDeadline.subject)}</span></div>`:'<p>Keine offene Frist</p>'}</div><div class="row-actions"><button class="icon-btn" data-edit="case" data-id="${c.id}" aria-label="Antrag bearbeiten">✎</button><button class="icon-btn danger" data-delete="cases" data-id="${c.id}" aria-label="Antrag löschen">×</button></div></div><div class="case-actions"><button class="secondary" data-add-correspondence="${c.id}">+ Korrespondenz</button><button class="secondary" data-add-case-event="${c.id}">+ Ereignis oder Frist</button><button class="secondary" data-add-child-case="${c.id}">+ Ergänzungsantrag</button></div>${events.length?`<section class="correspondence"><h3>Verlauf & Fristen</h3>${events.map(entry=>{const eventType=entry.eventType||'correspondence';const label=eventType==='deadline'?({legal:'Gesetzliche Frist',authority:'Behördenfrist',internal:'Interne Frist'}[entry.deadlineType]||'Frist'):eventType==='status'?'Statusänderung':eventType==='note'?'Notiz':({incoming:'Eingang',outgoing:'Ausgang',internal:'Interne Notiz'}[entry.direction]||'Korrespondenz');const deadline=eventType==='deadline'?deadlineStatus(entry):'';return `<article class="correspondence-item ${deadline?'deadline-'+deadline:''}"><span class="correspondence-direction">${esc(label)}</span><div class="grow"><strong>${esc(entry.subject)}</strong><p>${esc(entry.notes||'Keine Notiz')}</p>${eventType==='deadline'?`<p>${esc(entry.source||'Keine Grundlage angegeben')} · Erinnerung ${entry.reminderDays&&entry.reminderDays!=='none'?entry.reminderDays+' Tage vorher':'nicht gesetzt'} · ${esc(({open:'Offen',overdue:'Überschritten',met:'Eingehalten',cancelled:'Aufgehoben'}[deadline]||deadline))}</p>`:''}${attachmentLink(entry)}</div><time>${formatDate(entry.date)}</time><div class="row-actions">${eventType==='deadline'&&['open','overdue'].includes(deadline)?`<button class="icon-btn" data-deadline-task="${entry.id}" data-case-id="${c.id}" aria-label="Frist als Aufgabe anlegen">✓</button>`:''}<button class="icon-btn" data-edit="correspondence" data-id="${entry.id}">✎</button><button class="icon-btn danger" data-delete="correspondence" data-id="${entry.id}">×</button></div></article>`;}).join('')}</section>`:''}${children.length?`<section class="child-cases"><h3>Ergänzungsanträge</h3>${children.map(caseCard).join('')}</section>`:''}</article>`;};
  return `${header('Anträge & Behörden','Anträge','Anträge, Ereignisse, Fristen und Schriftverkehr als zusammenhängende Akte.',`<button class="primary" data-add="case">+ Neuer Antrag</button>`)}<div class="case-list">${topLevel.map(caseCard).join('')||empty('Noch kein Antrag vorhanden.')}</div>`;
}
function tasksPage(){
  const nextWeek=new Date();nextWeek.setDate(nextWeek.getDate()+7);const nextWeekValue=nextWeek.toISOString().slice(0,10);
  const matchesDue=t=>t.deletedAt||((taskFilters.due==='all'&&(!t.due||t.due>=today()))||(taskFilters.due==='past'&&t.due&&t.due<today())||(taskFilters.due==='overdue'&&t.due&&t.due<today()&&!['done','refused'].includes(t.status))||(taskFilters.due==='today'&&t.due===today())||(taskFilters.due==='week'&&t.due>=today()&&t.due<=nextWeekValue)||(taskFilters.due==='none'&&!t.due));
  const filtered=state.tasks.filter(t=>(taskFilters.category==='all'||(t.category||'')===taskFilters.category)&&(taskFilters.assignee==='all'||(t.assignee||'')===taskFilters.assignee)&&(taskFilters.status==='all'||t.status===taskFilters.status)&&matchesDue(t));
  const sorted=[...filtered].sort((a,b)=>(!!a.deletedAt)-(!!b.deletedAt)||(a.status==='done')-(b.status==='done')||(a.due||'9999').localeCompare(b.due||'9999'));
  const categories=[...new Set([...(state.taskOptions?.categories||[]),...state.tasks.map(t=>t.category).filter(Boolean)])];
  const filters=`<div class="card task-filters"><label>Kategorie<select data-task-filter="category"><option value="all">Alle Kategorien</option>${categories.map(value=>`<option ${taskFilters.category===value?'selected':''}>${esc(value)}</option>`)}</select></label><label>Verantwortlichkeit<select data-task-filter="assignee"><option value="all">Alle Personen</option>${state.members.map(m=>`<option value="${m.id}" ${taskFilters.assignee===m.id?'selected':''}>${esc(m.name)}</option>`)}</select></label><label>Fälligkeit<select data-task-filter="due">${options('all:Aktuelle & zukünftige|past:Vergangene Aufgaben|overdue:Überfällige offene|today:Heute|week:Nächste 7 Tage|none:Ohne Termin',taskFilters.due)}</select></label><label>Status<select data-task-filter="status">${options('all:Alle Status|planned:Geplant|open:Offen|done:Erledigt|refused:Linea wollte nicht',taskFilters.status)}</select></label></div>`;
  return `${header('Gemeinsam erledigen','Aufgaben','Klare Zuständigkeiten statt unübersichtlicher Absprachen.',`<button class="primary" data-add="task">+ Neue Aufgabe</button>`)}${filters}<div class="card data-card task-list">${sorted.map(t=>{const overdue=t.due&&t.due<today()&&!['done','refused'].includes(t.status);const history=(t.history||[]).map(h=>`<li>${formatDate(h.from)} → ${formatDate(h.to)} · ${esc(h.byName||userName(h.byUserId))}</li>`).join('');return `<article class="data-row ${overdue?'task-overdue':''} ${t.deletedAt?'task-deleted':''}"><div><h3>${t.status==='done'?'✓ ':''}${esc(t.title)}</h3><p>${esc(t.category||'Ohne Kategorie')} · ${t.recurrence&&t.recurrence!=='once'?'Wiederkehrend':'Einmalig'}</p><p>${esc(t.notes||'Keine Notiz')}</p>${history?`<details class="task-history"><summary>Terminverlauf (${t.history.length})</summary><ul>${history}</ul></details>`:''}${t.deletedAt?`<p class="deleted-note">Gelöscht von ${esc(t.deletedByName||userName(t.deletedByUserId))} – wartet auf endgültige Bestätigung.</p>`:''}</div><div><strong>${esc(memberName(t.assignee))}</strong><p>verantwortlich</p></div><div><span class="status ${statusClass(t.status)}">${esc(statusLabel(t.status))}</span><p>${formatDate(t.due)}</p>${overdue?'<strong class="overdue-label">Überfällig</strong>':''}</div><div class="row-actions">${t.deletedAt?`<button class="secondary danger" data-delete="tasks" data-id="${t.id}">Endgültig löschen</button>`:`<button class="icon-btn" data-edit="task" data-id="${t.id}" aria-label="Aufgabe bearbeiten">✎</button><button class="icon-btn danger" data-delete="tasks" data-id="${t.id}" aria-label="Aufgabe löschen">×</button>`}</div></article>`;}).join('')||empty('Keine Aufgaben entsprechen den Filtern.')}</div>`;
}
function documentsPage(){return `${header('Zentrale Ablage','Dokumente','Wichtige Unterlagen auffindbar erfassen.',`<button class="primary" data-add="document">+ Neues Dokument</button>`)}<p class="card" style="margin-bottom:20px">ⓘ Im MVP werden Dokument-Metadaten und der Ablageort erfasst. Ein sicherer Datei-Upload folgt in der nächsten Ausbaustufe.</p><div class="card data-card">${state.documents.map(d=>`<article class="data-row"><div><h3>${esc(d.title)}</h3><p>${esc(d.notes||'Keine Notiz')}</p></div><div><strong>${esc(d.category||'Sonstiges')}</strong><p>${esc(d.location||'Ablageort nicht angegeben')}</p></div><div><strong>${formatDate(d.date)}</strong><p>${esc(state.cases.find(c=>c.id===d.caseId)?.title||'Kein Antrag')}</p></div><div class="row-actions"><button class="icon-btn" data-edit="document" data-id="${d.id}">✎</button><button class="icon-btn danger" data-delete="documents" data-id="${d.id}">×</button></div></article>`).join('')||empty('Noch keine Dokumente erfasst.')}</div>`;}

function ledgerPage(){
  const selected=ledgerAccounts.has('__none__')?new Set():ledgerAccounts.size?ledgerAccounts:new Set(state.accounts.map(a=>a.id));
  const entries=state.ledger.filter(x=>selected.has(x.accountId));
  const sum=items=>items.reduce((s,x)=>s+(x.type==='income'?1:-1)*Number(x.amount||0),0);
  const balance=sum(entries);
  const accountCards=(state.accounts||[]).map(a=>`<div class="card mini-stat account-card"><span class="bubble" style="background:${esc(a.color)}22;color:${esc(a.color)}">€</span><div class="grow"><strong>${money(sum(state.ledger.filter(x=>x.accountId===a.id)))}</strong><small>${esc(a.name)}</small></div>${state.currentUser?.isAdmin?`<button class="icon-btn" data-edit="account" data-id="${a.id}" aria-label="${esc(a.name)} bearbeiten">✎</button>`:''}</div>`).join('');
  return `${header('Nachweise & Abrechnung','Kassenbuch','Einnahmen, Ausgaben und Belege nachvollziehbar dokumentieren.',`<button class="primary" data-add="ledger">+ Neue Buchung</button>`)}
  <section class="account-balances">${accountCards}</section>
  <div class="balance"><div><span>${selected.size===state.accounts.length?'Gesamtstand aller Konten':`Stand der ausgewählten Konten (${selected.size})`}</span><br><strong>${money(balance)}</strong></div><div class="balance-actions"><button class="secondary" data-export="csv">CSV / Excel</button> <button class="secondary" data-export="print">PDF drucken</button></div></div>
  <fieldset class="card account-filter"><legend>Kassenstände anzeigen</legend><label><input type="checkbox" data-account-all ${selected.size===state.accounts.length?'checked':''}> Alle Konten</label>${state.accounts.map(a=>`<label><input type="checkbox" data-account="${a.id}" ${selected.has(a.id)?'checked':''}> ${esc(a.name)}</label>`).join('')}</fieldset>
  <div class="card data-card ledger-list">${entries.map(x=>`<article class="data-row"><div><h3>${esc(x.description)}</h3><p>${esc(x.category||'Sonstiges')} · ${x.receiptStatus==='none'&&!x.receiptFile?'Kein Beleg':x.receipt?'Beleg '+esc(x.receipt):x.receiptFile?'Beleg vorhanden':'Kein Beleg angegeben'}</p></div><div><strong>${formatDate(x.date)}</strong><p>${esc(accountName(x.accountId))} · ${esc(x.payee||'Kein Empfänger')}</p><p>Eingetragen von ${esc(userName(x.createdByUserId,x.createdByName))}</p></div><div class="amount ${x.type==='income'?'income':'expense'}">${x.type==='income'?'+':'−'} ${money(x.amount)}${x.receiptFile?`<a class="receipt-link" href="/api/receipts/${encodeURIComponent(x.receiptFile)}" target="_blank">Beleg ansehen</a>`:''}</div>${x.receiptFile?`<figure class="receipt-print"><img src="/api/receipts/${encodeURIComponent(x.receiptFile)}" alt="Beleg zu ${esc(x.description)}"><figcaption>Beleg zu ${esc(x.description)}</figcaption></figure>`:''}<div class="row-actions"><button class="icon-btn" data-edit="ledger" data-id="${x.id}">✎</button><button class="icon-btn danger" data-delete="ledger" data-id="${x.id}">×</button></div></article>`).join('')||empty('Für diese Auswahl gibt es noch keine Buchungen.')}</div>`;
}
function accessSection(){
  if(!state.capabilities.manageAccess) return '';
  const labels={cases:'Anträge',tasks:'Aufgaben',documents:'Dokumente',ledger:'Kassenbuch',family:'Familie'};
  return `<div class="access-head"><h2 class="section-title">Zugänge & Berechtigungen</h2><button class="primary" data-user-add>+ Neuer Zugang</button></div><p class="access-note">Linea und die gesetzliche Betreuung dürfen Zugänge verwalten. Für alle anderen legst du die sichtbaren Bereiche einzeln fest.</p><div class="member-grid">${state.users.map(u=>`<article class="card user-card"><div><strong>${esc(u.displayName)}</strong><small>@${esc(u.username)} · ${esc(u.role)}</small></div><div class="permission-tags">${u.active===false?'<span class="status orange">Deaktiviert</span>':u.isAdmin?'<span class="status">Vollzugriff</span>':Object.entries(u.permissions||{}).filter(([,allowed])=>allowed).map(([key])=>`<span class="status gray">${labels[key]}</span>`).join('')||'<span class="status orange">Kein Bereich</span>'}</div><div class="row-actions"><button class="icon-btn" data-user-edit="${u.id}" aria-label="Zugang bearbeiten">✎</button>${u.id!==state.currentUser.id&&u.active!==false?`<button class="icon-btn danger" data-user-delete="${u.id}" aria-label="Zugang deaktivieren">×</button>`:''}</div></article>`).join('')}</div>`;
}
function familyPage(){return `${header('Zusammenarbeit','Familie','Wer arbeitet mit und darf Aufgaben übernehmen?',`<button class="primary" data-add="member">+ Neue Person</button>`)}<section class="card"><h2 class="section-title" style="margin-top:0">Familienbereich</h2><form id="family-form" class="form-grid"><div><label>Name des Bereichs</label><input name="name" value="${esc(state.family.name)}"></div><div><label>Name der leistungsberechtigten Person</label><input name="person" value="${esc(state.family.person)}"></div><div class="full form-actions"><button class="primary">Änderungen speichern</button></div></form></section><h2 class="section-title">Beteiligte Personen</h2><div class="member-grid">${state.members.map(m=>`<article class="card member"><div class="avatar" style="background:${esc(m.color)}">${esc(m.name.slice(0,1).toUpperCase())}</div><div class="grow"><strong>${esc(m.name)}</strong><small>${esc(m.role)}</small></div><button class="icon-btn" data-edit="member" data-id="${m.id}">✎</button></article>`).join('')}</div>${accessSection()}`;}

const configs={
  case:{collection:'cases',title:'Antrag',newTitle:'Neuer Antrag',fields:[['title','Titel','text'],['area','Antragsbereich','caseArea'],['authority','Behörde / Kontakt','text'],['assignee','Verantwortlich','members'],['status','Status','select','draft:Entwurf|collecting:Unterlagen sammeln|submitted:Eingereicht|question:Rückfrage|approved:Bewilligt|done:Erledigt'],['submittedAt','Versendet am','optionalDate'],['receivedAt','Bei der Stelle eingegangen am','optionalDate'],['parentCaseId','Zugehöriger Hauptantrag (optional)','cases'],['caseFileInput','Brief fotografieren oder PDF/Bild hochladen','casefile'],['description','Notiz','textarea']]},
  correspondence:{collection:'correspondence',title:'Verlaufseintrag',newTitle:'Neuer Verlaufseintrag',fields:[['caseId','Zugehöriger Antrag','cases'],['eventType','Art des Eintrags','select','correspondence:Korrespondenz|deadline:Frist|status:Statusänderung|note:Notiz'],['direction','Richtung','select','incoming:Eingang von Behörde|outgoing:Ausgang an Behörde|internal:Interne Notiz'],['date','Datum / Fristdatum','date'],['subject','Bezeichnung / Betreff','text'],['deadlineType','Fristart','select','legal:Gesetzliche Frist|authority:Von der Behörde gesetzt|internal:Interne Frist'],['deadlineStatus','Friststatus','select','open:Offen|met:Eingehalten|cancelled:Aufgehoben'],['reminderDays','Erinnerung','select','none:Keine Erinnerung|2:2 Tage vorher|7:7 Tage vorher|14:14 Tage vorher|30:30 Tage vorher'],['source','Grundlage / Quelle','text'],['caseFileInput','Brief fotografieren oder PDF/Bild hochladen','casefile'],['notes','Notiz','textarea']]},
  task:{collection:'tasks',title:'Aufgabe',newTitle:'Neue Aufgabe',fields:[['title','Aufgabe','text'],['category','Kategorie','taskSuggestions','categories'],['assignee','Verantwortlich','members'],['due','Fällig am','date'],['status','Status','select','planned:Geplant|open:Offen|done:Erledigt|refused:Linea wollte nicht'],['recurrence','Aufgabentyp','select','once:Einmalige Aufgabe|weekly:Wöchentlich wiederkehrend|biweekly:Alle zwei Wochen|monthly:Monatlich wiederkehrend|yearly:Jährlich wiederkehrend'],['recurrenceUntil','Wiederholen bis (optional)','date'],['notes','Notiz','textarea']]},
  document:{collection:'documents',title:'Dokument',newTitle:'Neues Dokument',fields:[['title','Dokumentname','text'],['category','Kategorie','select','Antrag:Antrag|Bescheid:Bescheid|Schriftverkehr:Schriftverkehr|Nachweis:Nachweis|Sonstiges:Sonstiges'],['date','Datum','date'],['caseId','Zugehöriger Antrag','cases'],['location','Ablageort / Aktenzeichen','text'],['notes','Notiz','textarea']]},
  ledger:{collection:'ledger',title:'Buchung',newTitle:'Neue Buchung',fields:[['description','Beschreibung','suggestions','descriptions'],['accountId','Konto','accounts'],['type','Art','select','expense:Ausgabe|income:Einnahme'],['amount','Betrag in Euro','money'],['date','Datum','date'],['category','Kategorie','suggestions','categories'],['payee','Empfänger / Quelle','text'],['receiptStatus','Beleg','select','available:Beleg vorhanden|none:Kein Beleg vorhanden'],['receipt','Belegnummer (optional)','text'],['receiptImage','Rechnung fotografieren oder hochladen','image'],['notes','Notiz','textarea']]},
  account:{collection:'accounts',title:'Konto',newTitle:'Neues Konto',fields:[['name','Kontoname','text'],['type','Kontoart','select','Bargeld:Bargeld|Bankkonto:Bankkonto'],['color','Farbe','color']]},
  member:{collection:'members',title:'Person',newTitle:'Neue Person',fields:[['name','Name','text'],['role','Rolle','select','Leistungsberechtigte Person:Leistungsberechtigte Person|Angehörige:Angehörige|Gesetzliche Betreuung:Gesetzliche Betreuung|Assistenz:Assistenz'],['color','Farbe','color']]}
};
function options(spec,value){return spec.split('|').map(x=>{const[v,l]=x.split(':');return `<option value="${esc(v)}" ${v===value?'selected':''}>${esc(l)}</option>`}).join('');}
function receiptDataUrl(file){
  return new Promise((resolve,reject)=>{
    const reader=new FileReader();
    reader.onerror=()=>reject(new Error('Das Bild konnte nicht gelesen werden.'));
    reader.onload=()=>{
      const image=new Image();
      image.onerror=()=>reject(new Error('Das Bildformat wird nicht unterstützt.'));
      image.onload=()=>{
        const scale=Math.min(1,1600/Math.max(image.width,image.height));
        const canvas=document.createElement('canvas');
        canvas.width=Math.round(image.width*scale);canvas.height=Math.round(image.height*scale);
        canvas.getContext('2d').drawImage(image,0,0,canvas.width,canvas.height);
        resolve(canvas.toDataURL('image/jpeg',.82));
      };
      image.src=reader.result;
    };
    reader.readAsDataURL(file);
  });
}
function caseFileDataUrl(file){
  if(file.type.startsWith('image/'))return receiptDataUrl(file);
  if(file.type!=='application/pdf')return Promise.reject(new Error('Bitte wähle ein Bild oder eine PDF-Datei aus.'));
  if(file.size>5_000_000)return Promise.reject(new Error('Die PDF-Datei darf höchstens 5 MB groß sein.'));
  return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onerror=()=>reject(new Error('Die Datei konnte nicht gelesen werden.'));reader.onload=()=>resolve(reader.result);reader.readAsDataURL(file);});
}
function openForm(type,id,defaults={}){
  const c=configs[type],item=id?state[c.collection].find(x=>x.id===id):{...defaults};
  if(type==='ledger'&&!item.receiptStatus) item.receiptStatus=item.receiptFile||item.receipt?'available':'none';
  if(type==='correspondence'&&!item.eventType)item.eventType='correspondence';
  const fields=c.fields.map(([name,label,kind,spec])=>{
    let input;
    if(kind==='textarea') input=`<textarea name="${name}">${esc(item[name]||'')}</textarea>`;
    else if(kind==='select') input=`<select name="${name}">${options(spec,item[name])}</select>`;
    else if(kind==='members') input=`<select name="${name}"><option value="">Nicht zugewiesen</option>${state.members.map(m=>`<option value="${m.id}" ${m.id===item[name]?'selected':''}>${esc(m.name)}</option>`)}</select>`;
    else if(kind==='accounts') input=`<select name="${name}" required>${state.accounts.map(a=>`<option value="${a.id}" ${a.id===(item[name]||state.accounts[0]?.id)?'selected':''}>${esc(a.name)}</option>`)}</select>`;
    else if(kind==='cases') input=`<select name="${name}"><option value="">Kein Antrag</option>${state.cases.filter(x=>x.id!==id).map(x=>`<option value="${x.id}" ${x.id===item[name]?'selected':''}>${esc(x.title)}</option>`)}</select>`;
    else if(kind==='image') input=`<input name="${name}" type="file" accept="image/jpeg,image/png,image/webp" capture="environment"><small class="field-help">Du kannst die Kamera öffnen oder ein vorhandenes Foto auswählen.${item.receiptFile?' Ein Beleg ist bereits hinterlegt.':''}</small>`;
    else if(kind==='casefile') input=`<input name="${name}" type="file" accept="image/jpeg,image/png,image/webp,application/pdf"><small class="field-help">Du kannst auf dem Smartphone einen Brief fotografieren oder ein Bild beziehungsweise eine PDF-Datei auswählen.${item.attachmentFile?' Ein Dokument ist bereits hinterlegt und wird bei einer neuen Auswahl ersetzt.':''}</small>`;
    else if(kind==='caseArea') input=`<input name="${name}" list="case-areas" value="${esc(item[name]||'')}"><datalist id="case-areas">${['Eingliederungshilfe','Pflege','Kurzzeitpflege','Arbeit','Wohnen','Stadt / Kommune','Gesundheit','Sonstiges'].map(value=>`<option value="${value}">`).join('')}</datalist>`;
    else if(kind==='optionalDate') input=`<input name="${name}" type="date" value="${esc(item[name]||'')}">`;
    else if(kind==='suggestions') input=`<input name="${name}" list="ledger-${spec}" value="${esc(item[name]||'')}" ${name==='description'?'required':''}><datalist id="ledger-${spec}">${(state.ledgerOptions?.[spec]||[]).map(value=>`<option value="${esc(value)}">`).join('')}</datalist><button type="button" class="suggestion-edit" data-manage-suggestions="${spec}">Vorschläge bearbeiten</button>`;
    else if(kind==='taskSuggestions') input=`<input name="${name}" list="task-${spec}" value="${esc(item[name]||'')}"><datalist id="task-${spec}">${(state.taskOptions?.[spec]||[]).map(value=>`<option value="${esc(value)}">`).join('')}</datalist><button type="button" class="suggestion-edit" data-manage-task-suggestions="${spec}">Vorschläge bearbeiten</button>`;
    else if(kind==='money') input=`<input name="${name}" type="number" min="0" step="0.01" inputmode="decimal" value="${esc(item[name]??'')}" required>`;
    else input=`<input name="${name}" type="${kind}" value="${esc(item[name]??(kind==='date'?today():kind==='color'?'#285c4d':''))}" ${name==='title'||name==='subject'||name==='description'&&type==='ledger'||name==='name'&&type==='account'?'required':''}>`;
    return `<div class="${['textarea','image','casefile'].includes(kind)?'full':''}"><label>${label}</label>${input}</div>`;
  }).join('');
  $('#modal-content').innerHTML=`<h2>${id?c.title+' bearbeiten':c.newTitle}</h2><form id="entry-form" class="form-grid">${fields}<div class="full form-actions"><button type="button" class="secondary" data-close>Abbrechen</button><button class="primary">Speichern</button></div></form>`;
  $('#modal').showModal();$('[data-close]').onclick=()=>$('#modal').close();
  $$('[data-manage-suggestions]',$('#entry-form')).forEach(button=>button.onclick=async()=>{
    const key=button.dataset.manageSuggestions,current=state.ledgerOptions?.[key]||[];
    const edited=prompt('Bearbeite die Vorschläge. Trenne mehrere Einträge mit einem Semikolon.',current.join('; '));
    if(edited===null)return;
    const values=[...new Set(edited.split(';').map(value=>value.trim()).filter(Boolean))];
    state.ledgerOptions=await request('/api/ledger-options',{method:'PUT',body:JSON.stringify({[key]:values})});
    const list=$(`#ledger-${key}`);list.innerHTML=values.map(value=>`<option value="${esc(value)}">`).join('');toast('Vorschläge aktualisiert.');
  });
  $$('[data-manage-task-suggestions]',$('#entry-form')).forEach(button=>button.onclick=async()=>{
    const key=button.dataset.manageTaskSuggestions,current=state.taskOptions?.[key]||[];
    const edited=prompt('Bearbeite die Kategorien. Trenne mehrere Einträge mit einem Semikolon.',current.join('; '));
    if(edited===null)return;
    const values=[...new Set(edited.split(';').map(value=>value.trim()).filter(Boolean))];
    state.taskOptions=await request('/api/task-options',{method:'PUT',body:JSON.stringify({[key]:values})});
    $(`#task-${key}`).innerHTML=values.map(value=>`<option value="${esc(value)}">`).join('');toast('Kategorien aktualisiert.');
  });
  const updateRecurrence=()=>{const once=$('[name="recurrence"]',$('#entry-form'))?.value==='once';$('[name="recurrenceUntil"]',$('#entry-form'))?.closest('div')?.classList.toggle('hidden',once);};
  $('[name="recurrence"]',$('#entry-form'))?.addEventListener('change',updateRecurrence);updateRecurrence();
  const updateReceiptFields=()=>{const none=$('[name="receiptStatus"]',$('#entry-form'))?.value==='none';['receipt','receiptImage'].forEach(name=>$(`[name="${name}"]`,$('#entry-form'))?.closest('div')?.classList.toggle('hidden',none));};
  $('[name="receiptStatus"]',$('#entry-form'))?.addEventListener('change',updateReceiptFields);updateReceiptFields();
  const updateCaseEventFields=()=>{const eventType=$('[name="eventType"]',$('#entry-form'))?.value;const deadline=eventType==='deadline';const correspondence=eventType==='correspondence';['direction'].forEach(name=>$(`[name="${name}"]`,$('#entry-form'))?.closest('div')?.classList.toggle('hidden',!correspondence));['deadlineType','deadlineStatus','reminderDays','source'].forEach(name=>$(`[name="${name}"]`,$('#entry-form'))?.closest('div')?.classList.toggle('hidden',!deadline));};
  $('[name="eventType"]',$('#entry-form'))?.addEventListener('change',updateCaseEventFields);updateCaseEventFields();
  $('#entry-form').onsubmit=async e=>{
    e.preventDefault();
    const formData=new FormData(e.target),file=formData.get('receiptImage'),caseFile=formData.get('caseFileInput');
    formData.delete('receiptImage');
    formData.delete('caseFileInput');
    const payload=Object.fromEntries(formData);
    if(file?.size) payload.receiptImage=await receiptDataUrl(file);
    if(caseFile?.size){payload.caseFile=await caseFileDataUrl(caseFile);payload.caseFileName=caseFile.name;}
    await request(`/api/${c.collection}${id?'/'+id:''}`,{method:id?'PUT':'POST',body:JSON.stringify(payload)});
    $('#modal').close();await refresh();toast('Gespeichert.');
  };
}
function openUserForm(id){
  const user=id?state.users.find(u=>u.id===id):{};
  const permissionLabels={cases:'Anträge',tasks:'Aufgaben',documents:'Dokumente',ledger:'Kassenbuch',family:'Familie'};
  $('#modal-content').innerHTML=`<h2>${id?'Zugang bearbeiten':'Neuer Zugang'}</h2><form id="user-form" class="form-grid">
    <div><label>Anzeigename</label><input name="displayName" value="${esc(user.displayName||'')}" required></div>
    <div><label>Benutzername</label><input name="username" value="${esc(user.username||'')}" ${id?'disabled':''} required></div>
    <div><label>Rolle</label><select name="role">${['Angehörige','Assistenz','Gesetzliche Betreuung','Leistungsberechtigte Person'].map(role=>`<option ${user.role===role?'selected':''}>${role}</option>`)}</select></div>
    <div><label>${id?'Neues Passwort (optional)':'Startpasswort'}</label><input name="password" type="password" minlength="8" ${id?'':'required'} autocomplete="new-password"></div>
    <fieldset class="full permissions"><legend>Zugriff auf Bereiche</legend>${Object.entries(permissionLabels).map(([key,label])=>`<label><input type="checkbox" name="permission_${key}" ${user.isAdmin||user.permissions?.[key]?'checked':''}> ${label}</label>`).join('')}<small>Linea und die gesetzliche Betreuung erhalten automatisch Vollzugriff.</small></fieldset>
    <div class="full form-actions"><button type="button" class="secondary" data-close>Abbrechen</button><button class="primary">Speichern</button></div>
  </form>`;
  $('#modal').showModal();$('[data-close]').onclick=()=>$('#modal').close();
  $('#user-form').onsubmit=async e=>{
    e.preventDefault();const form=new FormData(e.target);
    const payload={displayName:form.get('displayName'),username:form.get('username'),role:form.get('role'),password:form.get('password'),permissions:{}};
    Object.keys(permissionLabels).forEach(key=>payload.permissions[key]=form.has('permission_'+key));
    await request(`/api/users${id?'/'+id:''}`,{method:id?'PUT':'POST',body:JSON.stringify(payload)});
    $('#modal').close();await refresh();toast('Zugang gespeichert.');
  };
}

function visibleLedger(){const selected=ledgerAccounts.has('__none__')?new Set():ledgerAccounts.size?ledgerAccounts:new Set(state.accounts.map(a=>a.id));return state.ledger.filter(x=>selected.has(x.accountId));}
function exportCsv(){const rows=[['Datum','Konto','Art','Beschreibung','Kategorie','Empfänger/Quelle','Eingetragen von','Beleg','Belegbild','Betrag EUR'],...visibleLedger().map(x=>[x.date,accountName(x.accountId),x.type==='income'?'Einnahme':'Ausgabe',x.description,x.category,x.payee,userName(x.createdByUserId,x.createdByName),x.receiptStatus==='none'?'Kein Beleg':x.receipt,x.receiptFile?'Vorhanden':'',(x.type==='income'?1:-1)*Number(x.amount||0)])];const csv='\ufeff'+rows.map(r=>r.map(v=>`"${String(v??'').replaceAll('"','""')}"`).join(';')).join('\n');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));a.download=`kassenbuch-${today()}.csv`;a.click();URL.revokeObjectURL(a.href);}
function bind(){
  $$('[data-add]').forEach(b=>b.onclick=()=>openForm(b.dataset.add));$$('[data-edit]').forEach(b=>b.onclick=()=>openForm(b.dataset.edit,b.dataset.id));
  $$('[data-add-correspondence]').forEach(button=>button.onclick=()=>openForm('correspondence',null,{caseId:button.dataset.addCorrespondence}));
  $$('[data-add-case-event]').forEach(button=>button.onclick=()=>openForm('correspondence',null,{caseId:button.dataset.addCaseEvent,eventType:'deadline'}));
  $$('[data-add-child-case]').forEach(button=>button.onclick=()=>openForm('case',null,{parentCaseId:button.dataset.addChildCase}));
  $$('[data-deadline-task]').forEach(button=>button.onclick=async()=>{const entry=state.correspondence.find(item=>item.id===button.dataset.deadlineTask),application=state.cases.find(item=>item.id===button.dataset.caseId);if(!entry||!application)return;await request('/api/tasks',{method:'POST',body:JSON.stringify({title:`Frist: ${entry.subject} – ${application.title}`,category:'Anträge',assignee:application.assignee||'',due:entry.date<today()?today():entry.date,status:'planned',recurrence:'once',notes:`Ursprüngliches Fristdatum: ${formatDate(entry.date)}\n${entry.source||'Frist aus der Antragsakte'}\n${entry.notes||''}`})});await refresh();toast('Frist als Aufgabe angelegt.');});
  $$('[data-delete]').forEach(b=>b.onclick=async()=>{const final=b.textContent.includes('Endgültig');if(confirm(final?'Diese Aufgabe endgültig und unwiderruflich löschen?':'Diesen Eintrag wirklich löschen?')){const result=await request(`/api/${b.dataset.delete}/${b.dataset.id}`,{method:'DELETE'});await refresh();toast(result.pendingAdminConfirmation?'Zur Löschbestätigung vorgemerkt.':'Gelöscht.');}});
  $('[data-export="csv"]')?.addEventListener('click',exportCsv);$('[data-export="print"]')?.addEventListener('click',()=>window.print());
  $('[data-account-all]')?.addEventListener('change',e=>{ledgerAccounts=e.target.checked?new Set():new Set(['__none__']);render();});
  $$('[data-account]').forEach(input=>input.addEventListener('change',e=>{if(!ledgerAccounts.size)ledgerAccounts=new Set(state.accounts.map(a=>a.id));ledgerAccounts.delete('__none__');e.target.checked?ledgerAccounts.add(e.target.dataset.account):ledgerAccounts.delete(e.target.dataset.account);if(ledgerAccounts.size===state.accounts.length)ledgerAccounts=new Set();render();}));
  $$('[data-task-filter]').forEach(select=>select.addEventListener('change',e=>{taskFilters[e.target.dataset.taskFilter]=e.target.value;render();}));
  $('#family-form')?.addEventListener('submit',async e=>{e.preventDefault();await request('/api/family',{method:'PUT',body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});await refresh();toast('Familienbereich gespeichert.');});
  $('[data-user-add]')?.addEventListener('click',()=>openUserForm());
  $$('[data-user-edit]').forEach(button=>button.onclick=()=>openUserForm(button.dataset.userEdit));
  $$('[data-user-delete]').forEach(button=>button.onclick=async()=>{if(confirm('Diesen Zugang wirklich deaktivieren?')){await request('/api/users/'+button.dataset.userDelete,{method:'DELETE'});await refresh();toast('Zugang deaktiviert.');}});
}
function render(){if(!state)return;const permissions={cases:'cases',tasks:'tasks',documents:'documents',ledger:'ledger',family:'family'};let current=page();if(permissions[current]&&!state.capabilities[permissions[current]]){current='dashboard';location.hash='dashboard';}$$('[data-page]').forEach(a=>{const allowed=!permissions[a.dataset.page]||state.capabilities[permissions[a.dataset.page]];a.classList.toggle('hidden',!allowed);a.classList.toggle('active',a.dataset.page===current);});const views={dashboard,cases:casesPage,tasks:tasksPage,documents:documentsPage,ledger:ledgerPage,family:familyPage};$('#content').innerHTML=(views[current]||dashboard)();$('#content').focus({preventScroll:true});bind();}
start();
