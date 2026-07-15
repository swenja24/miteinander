const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const formatDate = value => value ? new Intl.DateTimeFormat('de-DE', {day:'2-digit',month:'short',year:'numeric'}).format(new Date(value + (value.length === 10 ? 'T12:00:00' : ''))) : 'Ohne Frist';
const money = value => new Intl.NumberFormat('de-DE',{style:'currency',currency:'EUR'}).format(Number(value)||0);
const today = () => new Date().toISOString().slice(0,10);
let state = null;
let ledgerAccount = 'all';

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
function statusLabel(status) { return ({draft:'Entwurf',collecting:'Unterlagen sammeln',submitted:'Eingereicht',question:'Rückfrage',approved:'Bewilligt',done:'Erledigt',open:'Offen'}[status] || status || 'Offen'); }
function statusClass(status) { return ['submitted','approved','done'].includes(status) ? '' : ['question','collecting'].includes(status) ? 'orange' : 'gray'; }

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
  const due=state.tasks.filter(t=>t.status!=='done'&&t.due).sort((a,b)=>a.due.localeCompare(b.due));
  const openCases=state.cases.filter(c=>!['approved','done'].includes(c.status));
  const balance=state.ledger.reduce((sum,x)=>sum+(x.type==='income'?1:-1)*Number(x.amount||0),0);
  return `${header(new Date().toLocaleDateString('de-DE',{weekday:'long',day:'numeric',month:'long'}),`Guten Tag, ${esc(state.currentUser?.displayName||state.family.person||'zusammen')}.`,'Hier siehst du, was gerade wichtig ist.')}
  <section class="hero"><div class="welcome"><p class="eyebrow">Gemeinsam den Überblick behalten</p><h2>Was steht als Nächstes an?</h2><p>${caps.tasks?(due.length?`„${esc(due[0].title)}“ ist die nächste anstehende Aufgabe.`:'Aktuell ist keine Aufgabe mit Frist offen. Zeit zum Durchatmen.'):'Du siehst hier nur die Bereiche, die für deinen Zugang freigegeben sind.'}</p>${caps.tasks?'<button class="primary" data-add="task">Aufgabe hinzufügen</button>':''}</div>${caps.tasks?`<div class="stat-card"><div><p class="eyebrow">Offene Fristen</p><div class="big">${due.length}</div><p>${due.filter(x=>x.due<today()).length} davon überfällig</p></div><a href="#tasks">Alle Aufgaben ansehen →</a></div>`:''}</section>
  <section class="stats">${caps.cases?`<div class="card mini-stat"><span class="bubble green">◇</span><div><strong>${openCases.length}</strong><small>laufende Anträge</small></div></div>`:''}${caps.documents?`<div class="card mini-stat"><span class="bubble purple">▤</span><div><strong>${state.documents.length}</strong><small>Dokumente erfasst</small></div></div>`:''}${caps.ledger?`<div class="card mini-stat"><span class="bubble orange">€</span><div><strong>${money(balance)}</strong><small>aktueller Kassenstand</small></div></div>`:''}</section>
  <section class="grid-2">${caps.tasks?`<div class="card"><div class="card-head"><h2>Nächste Aufgaben</h2><a class="link-btn" href="#tasks">Alle ansehen</a></div><div class="item-list">${due.slice(0,4).map(t=>`<div class="list-item">${dateBox(t.due)}<div class="grow"><strong>${esc(t.title)}</strong><small>${esc(memberName(t.assignee))}</small></div><span class="status ${t.due<today()?'orange':''}">${t.due<today()?'Überfällig':'Offen'}</span></div>`).join('')||empty('Noch keine Aufgaben vorhanden.')}</div></div>`:''}
  ${caps.cases?`<div class="card"><div class="card-head"><h2>Laufende Anträge</h2><a class="link-btn" href="#cases">Alle ansehen</a></div><div class="item-list">${state.cases.slice(0,4).map(c=>`<div class="list-item"><span class="bubble green">◇</span><div class="grow"><strong>${esc(c.title)}</strong><small>${esc(c.authority||'Keine Behörde angegeben')}</small></div><span class="status ${statusClass(c.status)}">${esc(statusLabel(c.status))}</span></div>`).join('')||empty('Lege den ersten Antrag an.')}</div></div>`:''}</section>`;
}

function casesPage(){return `${header('Anträge & Behörden','Anträge','Jeder Antrag mit Status, Frist und Zuständigkeit.',`<button class="primary" data-add="case">+ Neuer Antrag</button>`)}<div class="card data-card">${state.cases.map(c=>`<article class="data-row"><div><h3>${esc(c.title)}</h3><p>${esc(c.description||'Keine Notiz')}</p></div><div><strong>${esc(c.authority||'–')}</strong><p>Behörde / Kontakt</p></div><div><span class="status ${statusClass(c.status)}">${esc(statusLabel(c.status))}</span><p>${formatDate(c.due)}</p></div><div class="row-actions"><button class="icon-btn" data-edit="case" data-id="${c.id}" aria-label="Antrag bearbeiten">✎</button><button class="icon-btn danger" data-delete="cases" data-id="${c.id}" aria-label="Antrag löschen">×</button></div></article>`).join('')||empty('Noch kein Antrag vorhanden.')}</div>`;}
function tasksPage(){const sorted=[...state.tasks].sort((a,b)=>(a.status==='done')-(b.status==='done')||(a.due||'9999').localeCompare(b.due||'9999'));return `${header('Gemeinsam erledigen','Aufgaben','Klare Zuständigkeiten statt unübersichtlicher Absprachen.',`<button class="primary" data-add="task">+ Neue Aufgabe</button>`)}<div class="card data-card">${sorted.map(t=>`<article class="data-row"><div><h3>${t.status==='done'?'✓ ':''}${esc(t.title)}</h3><p>${esc(t.notes||'Keine Notiz')}</p></div><div><strong>${esc(memberName(t.assignee))}</strong><p>verantwortlich</p></div><div><span class="status ${statusClass(t.status)}">${esc(statusLabel(t.status))}</span><p>${formatDate(t.due)}</p></div><div class="row-actions"><button class="icon-btn" data-edit="task" data-id="${t.id}" aria-label="Aufgabe bearbeiten">✎</button><button class="icon-btn danger" data-delete="tasks" data-id="${t.id}" aria-label="Aufgabe löschen">×</button></div></article>`).join('')||empty('Noch keine Aufgaben vorhanden.')}</div>`;}
function documentsPage(){return `${header('Zentrale Ablage','Dokumente','Wichtige Unterlagen auffindbar erfassen.',`<button class="primary" data-add="document">+ Neues Dokument</button>`)}<p class="card" style="margin-bottom:20px">ⓘ Im MVP werden Dokument-Metadaten und der Ablageort erfasst. Ein sicherer Datei-Upload folgt in der nächsten Ausbaustufe.</p><div class="card data-card">${state.documents.map(d=>`<article class="data-row"><div><h3>${esc(d.title)}</h3><p>${esc(d.notes||'Keine Notiz')}</p></div><div><strong>${esc(d.category||'Sonstiges')}</strong><p>${esc(d.location||'Ablageort nicht angegeben')}</p></div><div><strong>${formatDate(d.date)}</strong><p>${esc(state.cases.find(c=>c.id===d.caseId)?.title||'Kein Antrag')}</p></div><div class="row-actions"><button class="icon-btn" data-edit="document" data-id="${d.id}">✎</button><button class="icon-btn danger" data-delete="documents" data-id="${d.id}">×</button></div></article>`).join('')||empty('Noch keine Dokumente erfasst.')}</div>`;}

function ledgerPage(){
  const entries=ledgerAccount==='all'?state.ledger:state.ledger.filter(x=>x.accountId===ledgerAccount);
  const sum=items=>items.reduce((s,x)=>s+(x.type==='income'?1:-1)*Number(x.amount||0),0);
  const balance=sum(entries);
  const accountCards=(state.accounts||[]).map(a=>`<div class="card mini-stat"><span class="bubble" style="background:${esc(a.color)}22;color:${esc(a.color)}">€</span><div><strong>${money(sum(state.ledger.filter(x=>x.accountId===a.id)))}</strong><small>${esc(a.name)}</small></div></div>`).join('');
  return `${header('Nachweise & Abrechnung','Kassenbuch','Einnahmen, Ausgaben und Belege nachvollziehbar dokumentieren.',`<button class="primary" data-add="ledger">+ Neue Buchung</button>`)}
  <section class="account-balances">${accountCards}</section>
  <div class="balance"><div><span>${ledgerAccount==='all'?'Gesamtstand aller Konten':esc(accountName(ledgerAccount))}</span><br><strong>${money(balance)}</strong></div><div class="balance-actions"><button class="secondary" data-export="csv">CSV / Excel</button> <button class="secondary" data-export="print">PDF drucken</button></div></div>
  <div class="toolbar"><label class="filter-label">Konto filtern<select id="account-filter"><option value="all">Alle Konten</option>${state.accounts.map(a=>`<option value="${a.id}" ${ledgerAccount===a.id?'selected':''}>${esc(a.name)}</option>`)}</select></label></div>
  <div class="card data-card">${entries.map(x=>`<article class="data-row"><div><h3>${esc(x.description)}</h3><p>${esc(x.category||'Sonstiges')}${x.receipt?' · Beleg '+esc(x.receipt):''}</p></div><div><strong>${formatDate(x.date)}</strong><p>${esc(accountName(x.accountId))} · ${esc(x.payee||'Kein Empfänger')}</p><p>Eingetragen von ${esc(userName(x.createdByUserId,x.createdByName))}</p></div><div class="amount ${x.type==='income'?'income':'expense'}">${x.type==='income'?'+':'−'} ${money(x.amount)}${x.receiptFile?`<a class="receipt-link" href="/api/receipts/${encodeURIComponent(x.receiptFile)}" target="_blank">Beleg ansehen</a>`:''}</div><div class="row-actions"><button class="icon-btn" data-edit="ledger" data-id="${x.id}">✎</button><button class="icon-btn danger" data-delete="ledger" data-id="${x.id}">×</button></div></article>`).join('')||empty('Für diese Auswahl gibt es noch keine Buchungen.')}</div>`;
}
function accessSection(){
  if(!state.capabilities.manageAccess) return '';
  const labels={cases:'Anträge',tasks:'Aufgaben',documents:'Dokumente',ledger:'Kassenbuch',family:'Familie'};
  return `<div class="access-head"><h2 class="section-title">Zugänge & Berechtigungen</h2><button class="primary" data-user-add>+ Neuer Zugang</button></div><p class="access-note">Linea und die gesetzliche Betreuung dürfen Zugänge verwalten. Für alle anderen legst du die sichtbaren Bereiche einzeln fest.</p><div class="member-grid">${state.users.map(u=>`<article class="card user-card"><div><strong>${esc(u.displayName)}</strong><small>@${esc(u.username)} · ${esc(u.role)}</small></div><div class="permission-tags">${u.active===false?'<span class="status orange">Deaktiviert</span>':u.isAdmin?'<span class="status">Vollzugriff</span>':Object.entries(u.permissions||{}).filter(([,allowed])=>allowed).map(([key])=>`<span class="status gray">${labels[key]}</span>`).join('')||'<span class="status orange">Kein Bereich</span>'}</div><div class="row-actions"><button class="icon-btn" data-user-edit="${u.id}" aria-label="Zugang bearbeiten">✎</button>${u.id!==state.currentUser.id&&u.active!==false?`<button class="icon-btn danger" data-user-delete="${u.id}" aria-label="Zugang deaktivieren">×</button>`:''}</div></article>`).join('')}</div>`;
}
function familyPage(){return `${header('Zusammenarbeit','Familie','Wer arbeitet mit und darf Aufgaben übernehmen?',`<button class="primary" data-add="member">+ Neue Person</button>`)}<section class="card"><h2 class="section-title" style="margin-top:0">Familienbereich</h2><form id="family-form" class="form-grid"><div><label>Name des Bereichs</label><input name="name" value="${esc(state.family.name)}"></div><div><label>Name der leistungsberechtigten Person</label><input name="person" value="${esc(state.family.person)}"></div><div class="full form-actions"><button class="primary">Änderungen speichern</button></div></form></section><h2 class="section-title">Beteiligte Personen</h2><div class="member-grid">${state.members.map(m=>`<article class="card member"><div class="avatar" style="background:${esc(m.color)}">${esc(m.name.slice(0,1).toUpperCase())}</div><div class="grow"><strong>${esc(m.name)}</strong><small>${esc(m.role)}</small></div><button class="icon-btn" data-edit="member" data-id="${m.id}">✎</button></article>`).join('')}</div>${accessSection()}`;}

const configs={
  case:{collection:'cases',title:'Antrag',newTitle:'Neuer Antrag',fields:[['title','Titel','text'],['authority','Behörde / Kontakt','text'],['status','Status','select','draft:Entwurf|collecting:Unterlagen sammeln|submitted:Eingereicht|question:Rückfrage|approved:Bewilligt|done:Erledigt'],['due','Nächste Frist','date'],['description','Notiz','textarea']]},
  task:{collection:'tasks',title:'Aufgabe',newTitle:'Neue Aufgabe',fields:[['title','Aufgabe','text'],['assignee','Verantwortlich','members'],['due','Fällig am','date'],['status','Status','select','open:Offen|done:Erledigt'],['notes','Notiz','textarea']]},
  document:{collection:'documents',title:'Dokument',newTitle:'Neues Dokument',fields:[['title','Dokumentname','text'],['category','Kategorie','select','Antrag:Antrag|Bescheid:Bescheid|Schriftverkehr:Schriftverkehr|Nachweis:Nachweis|Sonstiges:Sonstiges'],['date','Datum','date'],['caseId','Zugehöriger Antrag','cases'],['location','Ablageort / Aktenzeichen','text'],['notes','Notiz','textarea']]},
  ledger:{collection:'ledger',title:'Buchung',newTitle:'Neue Buchung',fields:[['description','Beschreibung','text'],['accountId','Konto','accounts'],['type','Art','select','expense:Ausgabe|income:Einnahme'],['amount','Betrag in Euro','number'],['date','Datum','date'],['category','Kategorie','text'],['payee','Empfänger / Quelle','text'],['receipt','Belegnummer','text'],['receiptImage','Rechnung fotografieren oder hochladen','image'],['notes','Notiz','textarea']]},
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
function openForm(type,id){
  const c=configs[type],item=id?state[c.collection].find(x=>x.id===id):{};
  const fields=c.fields.map(([name,label,kind,spec])=>{
    let input;
    if(kind==='textarea') input=`<textarea name="${name}">${esc(item[name]||'')}</textarea>`;
    else if(kind==='select') input=`<select name="${name}">${options(spec,item[name])}</select>`;
    else if(kind==='members') input=`<select name="${name}"><option value="">Nicht zugewiesen</option>${state.members.map(m=>`<option value="${m.id}" ${m.id===item[name]?'selected':''}>${esc(m.name)}</option>`)}</select>`;
    else if(kind==='accounts') input=`<select name="${name}" required>${state.accounts.map(a=>`<option value="${a.id}" ${a.id===(item[name]||state.accounts[0]?.id)?'selected':''}>${esc(a.name)}</option>`)}</select>`;
    else if(kind==='cases') input=`<select name="${name}"><option value="">Kein Antrag</option>${state.cases.map(x=>`<option value="${x.id}" ${x.id===item[name]?'selected':''}>${esc(x.title)}</option>`)}</select>`;
    else if(kind==='image') input=`<input name="${name}" type="file" accept="image/jpeg,image/png,image/webp" capture="environment"><small class="field-help">Du kannst die Kamera öffnen oder ein vorhandenes Foto auswählen.${item.receiptFile?' Ein Beleg ist bereits hinterlegt.':''}</small>`;
    else input=`<input name="${name}" type="${kind}" value="${esc(item[name]??(kind==='date'?today():kind==='color'?'#285c4d':''))}" ${name==='title'||name==='description'&&type==='ledger'?'required':''}>`;
    return `<div class="${['textarea','image'].includes(kind)?'full':''}"><label>${label}</label>${input}</div>`;
  }).join('');
  $('#modal-content').innerHTML=`<h2>${id?c.title+' bearbeiten':c.newTitle}</h2><form id="entry-form" class="form-grid">${fields}<div class="full form-actions"><button type="button" class="secondary" data-close>Abbrechen</button><button class="primary">Speichern</button></div></form>`;
  $('#modal').showModal();$('[data-close]').onclick=()=>$('#modal').close();
  $('#entry-form').onsubmit=async e=>{
    e.preventDefault();
    const formData=new FormData(e.target),file=formData.get('receiptImage');
    formData.delete('receiptImage');
    const payload=Object.fromEntries(formData);
    if(file?.size) payload.receiptImage=await receiptDataUrl(file);
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

function exportCsv(){const rows=[['Datum','Konto','Art','Beschreibung','Kategorie','Empfänger/Quelle','Eingetragen von','Beleg','Belegbild','Betrag EUR'],...state.ledger.map(x=>[x.date,accountName(x.accountId),x.type==='income'?'Einnahme':'Ausgabe',x.description,x.category,x.payee,userName(x.createdByUserId,x.createdByName),x.receipt,x.receiptFile?'Vorhanden':'',(x.type==='income'?1:-1)*Number(x.amount||0)])];const csv='\ufeff'+rows.map(r=>r.map(v=>`"${String(v??'').replaceAll('"','""')}"`).join(';')).join('\n');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));a.download=`kassenbuch-${today()}.csv`;a.click();URL.revokeObjectURL(a.href);}
function bind(){
  $$('[data-add]').forEach(b=>b.onclick=()=>openForm(b.dataset.add));$$('[data-edit]').forEach(b=>b.onclick=()=>openForm(b.dataset.edit,b.dataset.id));
  $$('[data-delete]').forEach(b=>b.onclick=async()=>{if(confirm('Diesen Eintrag wirklich löschen?')){await request(`/api/${b.dataset.delete}/${b.dataset.id}`,{method:'DELETE'});await refresh();toast('Gelöscht.');}});
  $('[data-export="csv"]')?.addEventListener('click',exportCsv);$('[data-export="print"]')?.addEventListener('click',()=>window.print());
  $('#account-filter')?.addEventListener('change',e=>{ledgerAccount=e.target.value;render();});
  $('#family-form')?.addEventListener('submit',async e=>{e.preventDefault();await request('/api/family',{method:'PUT',body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});await refresh();toast('Familienbereich gespeichert.');});
  $('[data-user-add]')?.addEventListener('click',()=>openUserForm());
  $$('[data-user-edit]').forEach(button=>button.onclick=()=>openUserForm(button.dataset.userEdit));
  $$('[data-user-delete]').forEach(button=>button.onclick=async()=>{if(confirm('Diesen Zugang wirklich deaktivieren?')){await request('/api/users/'+button.dataset.userDelete,{method:'DELETE'});await refresh();toast('Zugang deaktiviert.');}});
}
function render(){if(!state)return;const permissions={cases:'cases',tasks:'tasks',documents:'documents',ledger:'ledger',family:'family'};let current=page();if(permissions[current]&&!state.capabilities[permissions[current]]){current='dashboard';location.hash='dashboard';}$$('[data-page]').forEach(a=>{const allowed=!permissions[a.dataset.page]||state.capabilities[permissions[a.dataset.page]];a.classList.toggle('hidden',!allowed);a.classList.toggle('active',a.dataset.page===current);});const views={dashboard,cases:casesPage,tasks:tasksPage,documents:documentsPage,ledger:ledgerPage,family:familyPage};$('#content').innerHTML=(views[current]||dashboard)();$('#content').focus({preventScroll:true});bind();}
start();
