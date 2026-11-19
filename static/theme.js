// Runs synchronously in <head>: apply the saved theme before first paint (no flash).
(function () {
  var root = document.documentElement;
  root.classList.remove('no-js');
  root.classList.add('js');
  try {
    var t = localStorage.getItem('sto_theme');
    if (t === 'dark' || t === 'light') root.setAttribute('data-theme', t);
  } catch (e) { /* storage blocked: follow the system preference */ }
})();
