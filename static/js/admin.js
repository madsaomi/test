/* Админка: поиск по списку и отправка отчета (вынесено из HTML ради строгого CSP) */
// Поиск по загруженному списку (мгновенно, без запроса к серверу)
const search = document.getElementById('search');
search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    let shown = 0;
    document.querySelectorAll('#rows .item').forEach((el) => {
        const hit = !q || el.dataset.search.includes(q);
        el.hidden = !hit;
        if (hit) shown++;
    });
    const none = document.getElementById('no-match');
    if (none) none.hidden = shown > 0;
});

const btn = document.getElementById('report-btn');
btn.addEventListener('click', async () => {
    const note = document.getElementById('report-note');
    const period = document.getElementById('period-input').value.trim();
    if (!confirm('Отправить отчет за ' + period + ' всем админам в Telegram?')) return;
    btn.disabled = true; note.className = 'note'; note.textContent = 'Отправляем...';
    try {
        const res = await fetch('/admin/report?period=' + encodeURIComponent(period), { credentials: 'same-origin' });
        const data = await res.json();
        if (data.sent) { note.className = 'note ok'; note.textContent = '✓ Отчет отправлен в Telegram.'; }
        else { note.className = 'note err'; note.textContent = data.message || 'Telegram не принял отчет.'; }
    } catch (e) {
        note.className = 'note err'; note.textContent = 'Нет связи с сервером.';
    }
    btn.disabled = false;
});
