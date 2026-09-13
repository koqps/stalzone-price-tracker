(()=>{
  const placeholder=box=>{
    if(!box)return;
    box.querySelectorAll('img').forEach(img=>img.remove());
    if(!box.querySelector('.artifact-placeholder')){
      const s=document.createElement('span');
      s.className='artifact-placeholder';
      s.textContent='✦';
      s.setAttribute('aria-hidden','true');
      box.appendChild(s);
    }
  };
  const scan=root=>{
    (root||document).querySelectorAll?.('.icon').forEach(box=>{
      const img=box.querySelector('img');
      if(!img||!img.getAttribute('src'))placeholder(box);
    });
  };
  document.addEventListener('error',e=>{
    if(e.target instanceof HTMLImageElement&&e.target.closest('.icon'))placeholder(e.target.closest('.icon'));
  },true);
  new MutationObserver(ms=>ms.forEach(m=>m.addedNodes.forEach(n=>{
    if(n.nodeType===1)scan(n);
  }))).observe(document.documentElement,{childList:true,subtree:true});
  const style=document.createElement('style');
  style.textContent='.artifact-placeholder{font-size:28px;color:var(--accent);opacity:.72;line-height:1}.icon:has(.artifact-placeholder){background:radial-gradient(circle,color-mix(in srgb,var(--accent) 14%,transparent),transparent 70%),var(--panel2)}';
  document.head.appendChild(style);
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>scan(document));else scan(document);
})();