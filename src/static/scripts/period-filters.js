"use strict";
// Shared viewing-period controls; only custom ranges submit direct dates.
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('form[data-view-period-form]').forEach(form => {
        const select = form.querySelector('[data-period-choice]');
        const dates = form.querySelector('[data-period-dates]');
        const update = () => {
            const custom = select.value === 'custom';
            dates.hidden = !custom;
            dates.querySelectorAll('input').forEach(input => { input.disabled = !custom; });
        };
        // Calendar opts in with a server date in the application's named timezone.
        // Merely restoring a URL or changing period mode never inserts a date.
        if (form.dataset.periodToday) {
            const start = form.querySelector('[name="date_from"]');
            const end = form.querySelector('[name="date_to"]');
            let endTouched = Boolean(end.value);
            const dateKey = value => {
                const match = /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/.exec(value.trim());
                if (!match) return null;
                const [, d, m, y] = match.map(Number);
                const leap = y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0);
                const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
                if (y < 1 || m < 1 || m > 12 || d < 1 || d > days[m - 1]) return null;
                return y * 10000 + m * 100 + d;
            };
            const fillEnd = () => {
                const from = dateKey(start.value);
                const today = dateKey(form.dataset.periodToday);
                if (select.value === 'custom' && from && today && from <= today && !endTouched && !end.value) {
                    end.value = form.dataset.periodToday;
                }
            };
            ['input', 'change'].forEach(event => {
                end.addEventListener(event, () => { endTouched = true; });
                start.addEventListener(event, fillEnd);
            });
            form.addEventListener('reset', () => setTimeout(() => { endTouched = Boolean(end.value); }, 0));
        }
        select.addEventListener('change', update);
        form.addEventListener('reset', () => setTimeout(update, 0));
        // The profile's local search updates the address without submitting.
        // Carry its latest state through dates and pagination as well.
        const syncHistory = () => {
            const search = document.querySelector('#person-history-search');
            const hiddenSearch = form.querySelector('[name="history_q"]');
            if (search && hiddenSearch) hiddenSearch.value = search.value;
        };
        form.addEventListener('submit', syncHistory);
        const search = document.querySelector('#person-history-search');
        if (search) {
            const updateLinks = () => document.querySelectorAll('[data-history-page-link]').forEach(link => {
                const target = new URL(link.href, location.origin);
                if (search.value) target.searchParams.set('history_q', search.value);
                else target.searchParams.delete('history_q');
                link.href = target.pathname + target.search;
            });
            search.addEventListener('input', updateLinks);
            document.querySelectorAll('[data-history-page-link]').forEach(link => link.addEventListener('click', updateLinks));
        }
        document.querySelectorAll('[data-person-event-link]').forEach(link => {
            link.addEventListener('click', () => {
                const eventURL = new URL(link.href, location.origin);
                const back = new URL(location.href);
                back.searchParams.set('tab', 'history');
                back.hash = '';
                eventURL.searchParams.set('return', back.pathname + back.search);
                link.href = eventURL.pathname + eventURL.search;
            });
        });
        update();
    });
});
