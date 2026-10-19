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
