1) Sube `widget.js` y `widget.css` junto con el backend (Flask los expone en /static/…).
2) En Shopify → Online Store → Themes → Edit code → layout/theme.liquid, agrega ANTES del cierre de </body>:


<script>
window.MAXTER_BASE_URL = 'https://YOUR-RENDER-APP.onrender.com';
</script>
<script defer src="https://YOUR-RENDER-APP.onrender.com/static/widget.js"></script>


3) Publica el tema. El botón “Ayuda Master” aparecerá flotando.