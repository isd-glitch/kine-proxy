const form = document.getElementById('proxy-form');
const urlInput = document.getElementById('target-url');
const methodSelect = document.getElementById('http-method');
const headersInput = document.getElementById('custom-headers');
const bodyInput = document.getElementById('request-body');
const showIframe = document.getElementById('show-iframe');
const showRaw = document.getElementById('show-raw');
const contentPre = document.getElementById('proxy-content');
const frame = document.getElementById('proxy-frame');
const historyList = document.getElementById('history-list');
const bookmarkBtn = document.getElementById('bookmark-btn');
const bookmarkList = document.getElementById('bookmark-list');
const resetBtn = document.getElementById('reset-btn');

let historyArr = JSON.parse(localStorage.getItem('proxyHistory')||'[]');
let bookmarkArr = JSON.parse(localStorage.getItem('proxyBookmarks')||'[]');

function updateHistory(url) {
  if (!historyArr.includes(url)) {
    historyArr.unshift(url);
    if (historyArr.length > 20) historyArr.pop();
    localStorage.setItem('proxyHistory', JSON.stringify(historyArr));
    renderHistory();
  }
}
function renderHistory() {
  historyList.innerHTML = '';
  historyArr.forEach(u => {
    const opt = document.createElement('option');
    opt.value = u; opt.textContent = u;
    historyList.appendChild(opt);
  });
}
function renderBookmarks() {
  bookmarkList.innerHTML = '';
  bookmarkArr.forEach(u => {
    const opt = document.createElement('option');
    opt.value = u; opt.textContent = u;
    bookmarkList.appendChild(opt);
  });
}
renderHistory();
renderBookmarks();

historyList.addEventListener('change',()=>{
  urlInput.value = historyList.value;
});
bookmarkList.addEventListener('change',()=>{
  urlInput.value = bookmarkList.value;
});
bookmarkBtn.addEventListener('click',()=>{
  const url = urlInput.value;
  if(url && !bookmarkArr.includes(url)){
    bookmarkArr.unshift(url);
    if(bookmarkArr.length>20) bookmarkArr.pop();
    localStorage.setItem('proxyBookmarks', JSON.stringify(bookmarkArr));
    renderBookmarks();
  }
});
resetBtn.addEventListener('click',()=>{
  urlInput.value = '';
  headersInput.value = '';
  bodyInput.value = '';
  contentPre.textContent = '';
  frame.style.display = 'none';
});

form.addEventListener('submit', async function(e) {
  e.preventDefault();
  const url = urlInput.value;
  const method = methodSelect.value;
  const headersRaw = headersInput.value.trim();
  const body = bodyInput.value;
  const proxyUrl = `https://corsproxy.io/?${encodeURIComponent(url)}`;
  contentPre.textContent = '読み込み中...';
  frame.style.display = 'none';
  updateHistory(url);

  let headers = {};
  if (headersRaw) {
    headersRaw.split('\n').forEach(line => {
      const idx = line.indexOf(':');
      if (idx > 0) {
        const key = line.slice(0, idx).trim();
        const val = line.slice(idx+1).trim();
        if (key) headers[key] = val;
      }
    });
  }
  let fetchOpt = { method, headers };
  if (["POST","PUT"].includes(method)) {
    fetchOpt.body = body;
  }
  try {
    const res = await fetch(proxyUrl, fetchOpt);
    const contentType = res.headers.get('content-type') || '';
    if ((showIframe.checked || (contentType.includes('text/html') && !showRaw.checked))) {
      frame.src = proxyUrl;
      frame.style.display = 'block';
      contentPre.textContent = '';
    } else {
      const text = await res.text();
      contentPre.textContent = showRaw.checked ? text : (contentType.includes('application/json') ? JSON.stringify(JSON.parse(text), null, 2) : text);
      frame.style.display = 'none';
    }
  } catch (err) {
    contentPre.textContent = 'エラー: ' + err.message;
    frame.style.display = 'none';
  }
});
