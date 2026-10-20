'use strict';
document.querySelectorAll('[data-team-roster]').forEach((panel) => {
    const search = panel.querySelector('[data-team-search]');
    const count = panel.querySelector('[data-team-selected]');
    const update = () => {
        const query = search.value.toLocaleLowerCase().trim();
        panel.querySelectorAll('[data-team-row]').forEach((row) => {
            row.hidden = !row.textContent.toLocaleLowerCase().includes(query);
        });
        if (count) count.textContent = `Выбрано: ${panel.querySelectorAll('input[type="checkbox"]:checked').length}`;
    };
    search.addEventListener('input', update);
    panel.addEventListener('change', update);
    update();
});
