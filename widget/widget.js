(function(){
</div>`;
document.body.appendChild(panel);


const messages = panel.querySelector('#maxter-messages');
const text = panel.querySelector('#maxter-text');


function pushMessage(html, cls){
const div = document.createElement('div');
div.className = 'maxter-msg ' + cls;
div.innerHTML = html;
messages.appendChild(div);
messages.scrollTop = messages.scrollHeight;
}


function renderCards(products){
products.forEach(p => {
const card = document.createElement('div');
card.className = 'maxter-card';
card.innerHTML = `
<img src="${p.image}" alt="${p.title}">
<div class="meta">
<h4>${p.title}</h4>
<div class="maxter-price">
${p.compare_at_price ? `<s>${p.compare_at_price}</s>` : ''}
<span>${p.price}</span>
</div>
<div class="maxter-actions">
<a class="maxter-btn" href="${p.buy_url}" target="_blank" rel="noopener">Comprar ahora</a>
<a class="maxter-link" href="${p.product_url}" target="_blank" rel="noopener">Ver producto</a>
</div>
<div class="maxter-inv">
<div><strong>Inventario:</strong></div>
<ul style="margin:6px 0 0 16px; padding:0;">
${p.inventory.map(i => `<li>${i.location}: ${i.available} disponibles</li>`).join('')}
</ul>
</div>
</div>`;
messages.appendChild(card);
});
}


async function send(){
const q = text.value.trim();
if(!q) return;
pushMessage(q, 'maxter-user');
text.value = '';


try{
const r = await fetch(BASE_URL + '/api/chat', {
method: 'POST', headers: {'Content-Type': 'application/json'},
body: JSON.stringify({message: q})
});
const data = await r.json();
pushMessage(data.answer, 'maxter-bot');
renderCards(data.products || []);
}catch(e){
pushMessage('Hubo un problema temporal. Intenta de nuevo.', 'maxter-bot');
}
}


// Voz a texto (Web Speech API)
let rec = null; let listening = false;
const mic = panel.querySelector('#maxter-mic');
if('webkitSpeechRecognition' in window){
rec = new webkitSpeechRecognition();
rec.continuous = false; rec.interimResults = false; rec.lang = 'es-MX';
rec.onresult = (e)=>{ text.value = e.results[0][0].transcript; };
rec.onend = ()=>{ listening=false; mic.innerText='🎙️'; };
mic.addEventListener('click', ()=>{
if(listening){ rec.stop(); return; }
listening = true; mic.innerText='⏹️'; rec.start();
});
}else{
mic.title = 'Voz no soportada en este navegador';
}


// Eventos UI
btn.addEventListener('click', ()=>{ panel.style.display = 'flex'; });
panel.querySelector('#maxter-close').addEventListener('click', ()=>{ panel.style.display = 'none'; });
panel.querySelector('#maxter-send').addEventListener('click', send);
text.addEventListener('keydown', (e)=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); send(); } });
})();