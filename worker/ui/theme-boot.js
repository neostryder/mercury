// Runs before the page paints so a saved theme and motion setting apply without a flash.
(function () {
  var root = document.documentElement;
  try {
    var theme = localStorage.getItem('mercury-theme');
    if (theme === 'light' || theme === 'dark') root.setAttribute('data-theme', theme);
    var motion = localStorage.getItem('mercury-motion');
    if (motion === 'on' || motion === 'off') root.setAttribute('data-motion', motion);
  } catch (err) { /* storage can be blocked */ }
  var colors = { light: '#f4efe3', dark: '#101624' };
  var forced = root.getAttribute('data-theme');
  if (forced) {
    document.getElementById('theme-color-light').content = colors[forced];
    document.getElementById('theme-color-dark').content = colors[forced];
  }
})();
