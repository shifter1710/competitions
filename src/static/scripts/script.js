// Комбобокс инлайн-строки (№4): input + кнопка-стрелка + выпадающий список.
// Ввод с клавиатуры фильтрует список по подстроке, значение можно ввести
// свободно — подсказка, не валидация. Список живёт в <body> с position:
// fixed: таблица лежит в .table-responsive (overflow: auto), абсолютное
// позиционирование внутри ячейки обрезалось бы контейнером.
class InlineComboBox {
    constructor(input, toggleButton, getOptions) {
        this.input = input;
        this.toggleButton = toggleButton;
        this.getOptions = getOptions;
        this.open = false;
        this.highlightIndex = -1;
        this.closeTimer = null;
        this.dropdown = document.createElement("ul");
        this.dropdown.className = "combobox-dropdown d-none";
        document.body.appendChild(this.dropdown);
        this.handleOutsideMousedown = (event) => {
            if (
                this.open &&
                event.target !== this.input &&
                !this.toggleButton.contains(event.target) &&
                !this.dropdown.contains(event.target)
            ) {
                this.close();
            }
        };
        this.reposition = () => this.positionDropdown();
        document.addEventListener("mousedown", this.handleOutsideMousedown);
        input.addEventListener("input", (event) => {
            // Синтетический input после выбора из списка не должен открывать
            // список заново — он нужен только связке институт→группа.
            if (!event.isTrusted) {
                return;
            }
            this.openDropdown();
        });
        input.addEventListener("keydown", (event) => this.handleKeydown(event));
        input.addEventListener("blur", () => {
            this.closeTimer = setTimeout(() => this.close(), 120);
        });
        toggleButton.addEventListener("click", (event) => {
            event.preventDefault();
            clearTimeout(this.closeTimer);
            if (this.open) {
                this.close();
            } else {
                this.openDropdown();
            }
            this.input.focus();
        });
        this.dropdown.addEventListener("mousedown", (event) => {
            const option = event.target.closest(".combobox-option");
            if (!option) {
                return;
            }
            event.preventDefault(); // фокус остаётся в поле до выбора значения
            this.select(option.dataset.value);
        });
    }

    isOpen() {
        return this.open;
    }

    openDropdown() {
        this.renderOptions();
        this.open = true;
        this.dropdown.classList.remove("d-none");
        this.positionDropdown();
        window.addEventListener("scroll", this.reposition, true);
        window.addEventListener("resize", this.reposition);
    }

    close() {
        this.open = false;
        this.highlightIndex = -1;
        this.dropdown.classList.add("d-none");
        window.removeEventListener("scroll", this.reposition, true);
        window.removeEventListener("resize", this.reposition);
    }

    refresh() {
        if (!this.open) {
            return;
        }
        this.renderOptions();
        this.positionDropdown();
    }

    filteredOptions() {
        const query = this.input.value.trim().toLowerCase();
        const options = this.getOptions() || [];
        if (!query) {
            return options;
        }
        return options.filter((option) => option.toLowerCase().includes(query));
    }

    renderOptions() {
        const options = this.filteredOptions();
        if (!options.length) {
            const empty = document.createElement("li");
            empty.className = "combobox-empty";
            empty.textContent = "Нет совпадений — можно ввести своё значение";
            this.dropdown.replaceChildren(empty);
            this.highlightIndex = -1;
            return;
        }
        this.highlightIndex = Math.min(this.highlightIndex, options.length - 1);
        this.dropdown.replaceChildren(
            ...options.map((value) => {
                const item = document.createElement("li");
                item.className = "combobox-option";
                item.dataset.value = value;
                item.textContent = value;
                item.setAttribute("role", "option");
                return item;
            })
        );
        this.updateHighlight();
    }

    visibleOptions() {
        return Array.from(this.dropdown.querySelectorAll(".combobox-option"));
    }

    updateHighlight() {
        const options = this.visibleOptions();
        options.forEach((item, index) => {
            item.classList.toggle("combobox-option-active", index === this.highlightIndex);
        });
        const active = options[this.highlightIndex];
        if (active) {
            active.scrollIntoView({block: "nearest"});
        }
    }

    positionDropdown() {
        if (!this.open) {
            return;
        }
        const rect = this.input.getBoundingClientRect();
        this.dropdown.style.left = `${rect.left}px`;
        this.dropdown.style.top = `${rect.bottom + 2}px`;
        this.dropdown.style.minWidth = `${rect.width}px`;
    }

    handleKeydown(event) {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            event.stopPropagation();
            if (!this.open) {
                this.openDropdown();
                return;
            }
            const options = this.visibleOptions();
            if (!options.length) {
                return;
            }
            const direction = event.key === "ArrowDown" ? 1 : -1;
            this.highlightIndex = (this.highlightIndex + direction + options.length) % options.length;
            this.updateHighlight();
            return;
        }
        if (event.key === "Enter" && this.open) {
            // Открытый список перехватывает Enter: выбор подсвеченного (или
            // просто закрытие) — сохранение строки не срабатывает.
            event.preventDefault();
            event.stopPropagation();
            if (this.highlightIndex >= 0) {
                this.select(this.visibleOptions()[this.highlightIndex].dataset.value);
            } else {
                this.close();
            }
            return;
        }
        if (event.key === "Escape" && this.open) {
            // Escape сначала закрывает список; повторный — отменяет правку.
            event.preventDefault();
            event.stopPropagation();
            this.close();
        }
    }

    // Строковый обработчик Enter/Escape (capture-фаза на <tr>) спрашивает
    // виджет, открыт ли список: открытое меню работает в два шага.
    isOpen() {
        return this.open;
    }

    select(value) {
        this.input.value = value;
        this.close();
        // Выбор из списка — тоже изменение значения: связка институт→группа
        // слушает input и перезаполнит список групп.
        this.input.dispatchEvent(new Event("input", {bubbles: true}));
    }

    destroy() {
        clearTimeout(this.closeTimer);
        this.close();
        document.removeEventListener("mousedown", this.handleOutsideMousedown);
        this.dropdown.remove();
    }
}

// Уведомления (notify, IA-редизайн 2026-09): единая замена модальных
// браузерных диалогов для результатов фоновых действий (сохранение,
// импорт, удаление, модерация).
// Стек карточек в правом верхнем углу; текст вставляется через textContent;
// тип задаёт рамку/полосу/иконку по токенам main.css (--ok/--warn/--err/
// --info). Автоскрытие: success/info 5 с, warning 8 с, error 10 с, с паузой
// на hover/focus; Esc закрывает карточку в фокусе; в стеке не больше 4.
// Опциональна одна кнопка-действие (ссылка actionUrl или обработчик
// actionHandler). Функция глобальная: её зовут Main.makeRequest и
// ParticipantsPage.request.

const NOTIFY_TIMEOUT_MS = {success: 5000, info: 5000, warning: 8000, error: 10000};
const NOTIFY_STACK_LIMIT = 4;
const NOTIFY_ICONS = {
    success: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor"'
        + ' stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.3"/>'
        + '<path d="M5.3 8.2l1.8 1.8 3.6-3.9"/></svg>',
    warning: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor"'
        + ' stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2.4L14 13H2z"/>'
        + '<path d="M8 6.4v3"/><circle cx="8" cy="11.3" r="0.1"/></svg>',
    error: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor"'
        + ' stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.3"/>'
        + '<path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4"/></svg>',
    info: '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor"'
        + ' stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="8" cy="8" r="6.3"/>'
        + '<path d="M8 7.4v3.4"/><circle cx="8" cy="5.2" r="0.1"/></svg>',
};

function ensureNotifyStack() {
    let stack = document.getElementById("notify-stack");
    if (!stack) {
        stack = document.createElement("div");
        stack.id = "notify-stack";
        stack.className = "notify-stack";
        stack.setAttribute("aria-live", "polite");
        document.body.appendChild(stack);
    }
    return stack;
}

function notify({
    type = "info",
    text = "",
    actionLabel = "",
    actionUrl = "",
    actionHandler = null,
    timeoutMs,
} = {}) {
    const kind = Object.prototype.hasOwnProperty.call(NOTIFY_TIMEOUT_MS, type) ? type : "info";
    const stack = ensureNotifyStack();
    while (stack.children.length >= NOTIFY_STACK_LIMIT) {
        stack.firstElementChild.remove();
    }

    const card = document.createElement("div");
    card.className = `notify-card notify-card--${kind}`;
    // Спокойные типы не перебивают скринридер (status), тревожные — alert.
    card.setAttribute("role", kind === "success" || kind === "info" ? "status" : "alert");

    const icon = document.createElement("span");
    icon.className = "notify-card__icon";
    icon.innerHTML = NOTIFY_ICONS[kind];
    icon.setAttribute("aria-hidden", "true");

    const message = document.createElement("span");
    message.className = "notify-card__text";
    message.textContent = text;
    card.append(icon, message);

    let hideTimer = null;
    let startedAt = 0;
    let remaining = timeoutMs ?? NOTIFY_TIMEOUT_MS[kind];
    const dismiss = () => {
        clearTimeout(hideTimer);
        hideTimer = null;
        card.remove();
    };
    const scheduleHide = () => {
        clearTimeout(hideTimer);
        if (remaining <= 0) {
            dismiss();
            return;
        }
        startedAt = Date.now();
        hideTimer = setTimeout(dismiss, remaining);
    };
    const pauseHide = () => {
        if (hideTimer === null) {
            return;
        }
        clearTimeout(hideTimer);
        remaining -= Date.now() - startedAt;
        hideTimer = null;
    };

    if (actionLabel) {
        if (actionUrl) {
            const link = document.createElement("a");
            link.className = "btn btn-sm btn-outline-secondary notify-card__action";
            link.href = actionUrl;
            link.textContent = actionLabel;
            card.append(link);
        } else {
            const actionButton = document.createElement("button");
            actionButton.type = "button";
            actionButton.className = "btn btn-sm btn-outline-secondary notify-card__action";
            actionButton.textContent = actionLabel;
            actionButton.addEventListener("click", () => {
                if (typeof actionHandler === "function") {
                    actionHandler();
                }
                dismiss();
            });
            card.append(actionButton);
        }
    }

    const closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.className = "notify-card__close";
    closeButton.setAttribute("aria-label", "Закрыть");
    closeButton.textContent = "✕";
    closeButton.addEventListener("click", dismiss);
    card.append(closeButton);

    card.addEventListener("mouseenter", pauseHide);
    card.addEventListener("focusin", pauseHide);
    card.addEventListener("mouseleave", scheduleHide);
    card.addEventListener("focusout", (event) => {
        if (!card.contains(event.relatedTarget)) {
            scheduleHide();
        }
    });
    card.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            event.stopPropagation();
            dismiss();
        }
    });

    stack.append(card);
    scheduleHide();
    return dismiss;
}

window.notify = notify;

// Серверные flash-сообщения (base.html): success скрывается сам через 10 с
// (danger остаётся до ручного закрытия). Закрытие — нативный btn-close
// Bootstrap, здесь только автоскрытие.
function initFlashAutoHide() {
    document.querySelectorAll("[data-flash-autohide]").forEach((element) => {
        setTimeout(() => element.remove(), 10000);
    });
}

// Repeater ссылок соревнования (Multiple Event Links): 0..N пар «Название +
// URL» в формах создания/правки события (calendar.html, calendar_event.html,
// competition_link.html). Делегированный обработчик на document — одна
// привязка на страницу (прецедент initUsersPage), работает и внутри
// collapse-форм. Кап LINK_ROWS_MAX дублирует серверный MAX_EVENT_LINKS
// (превышение — 400 от сервера); «Удалить» не отключается никогда — 0 строк
// валиден. Enter в инпутах строки — нативный сабмит формы.
const LINK_ROWS_MAX = 20;

function refreshLinksAddButton(field) {
    const count = field.querySelectorAll("[data-links-row]").length;
    const addButton = field.querySelector("[data-links-add]");
    if (!addButton) {
        return;
    }
    addButton.disabled = count >= LINK_ROWS_MAX;
    addButton.title = count >= LINK_ROWS_MAX ? `Максимум ${LINK_ROWS_MAX} ссылок` : "";
}

function initEventLinksRepeaters() {
    // Начальное состояние капа (например, правка события с 20 ссылками).
    document.querySelectorAll("[data-links-field]").forEach(refreshLinksAddButton);
    document.addEventListener("click", (event) => {
        const addButton = event.target.closest("[data-links-add]");
        if (addButton) {
            const field = addButton.closest("[data-links-field]");
            const rows = field.querySelector("[data-links-rows]");
            const template = field.querySelector("[data-links-row-template]");
            if (!field || !rows || !template) {
                return;
            }
            rows.append(template.content.firstElementChild.cloneNode(true));
            rows.lastElementChild.querySelector('[name="link_label"]').focus();
            refreshLinksAddButton(field);
            return;
        }
        const removeButton = event.target.closest("[data-links-remove]");
        if (removeButton) {
            const field = removeButton.closest("[data-links-field]");
            removeButton.closest("[data-links-row]").remove();
            refreshLinksAddButton(field);
        }
    });
    // «Отмена» (type="reset") чистит значения, но не убирает динамически
    // добавленные строки — оставляем первую; значения вернёт нативный reset
    // (событие срабатывает до него).
    document.querySelectorAll("form").forEach((form) => {
        if (!form.querySelector("[data-links-field]")) {
            return;
        }
        form.addEventListener("reset", () => {
            form.querySelectorAll("[data-links-field]").forEach((field) => {
                field.querySelectorAll("[data-links-row]").forEach((row, index) => {
                    if (index > 0) {
                        row.remove();
                    }
                });
                refreshLinksAddButton(field);
            });
        });
    });
}

// Резолвер атлета (№23доп, docs/feedback-live.md): тихий поиск по ФИО в
// инлайн-строке главной. Ввод ≥2 символов — запрос /api/athletes/search
// (троттлинг + отмена, ошибки сети тихие), выбор варианта подставляет
// известные sex/institute/group/course в строку; поля остаются
// редактируемыми — автоподстановка, не принуждение. Свободный ввод ФИО
// без выбора — как раньше. Список живёт в <body> с position: fixed, как у
// InlineComboBox: таблица в .table-responsive обрезала бы абсолют.
// Вызовы только у ролей с правом записи: атлету чужие ФИО не раскрываются
// (эндпоинт отвечает 403, его автоподстановка из профиля уже работает).
//
// Варианты двух видов: kind === "student" — активная карточка из раздела
// Students (вторая строка с мета-данными и бейджем «карточка»), прочие —
// легаси-подсказки по истории участий. Опция {students: true} включает
// карточки (страница участника события: явный выбор пишет student_ref_id);
// без неё (инлайн-строки главной) карточки фильтруются — POST /competition
// связь не пишет, и показывать её не нужно. Опция {onlyStudents: true}
// (поиск на странице «Студенты») оставляет ТОЛЬКО карточки — легаси-подсказки
// там не нужны: список и так перед глазами.
class FioResolver {
    constructor(input, onSelect, options = {}) {
        this.input = input;
        this.onSelect = onSelect;
        this.includeStudents = options.students === true;
        this.onlyStudents = options.onlyStudents === true;
        this.open = false;
        this.highlightIndex = -1;
        this.closeTimer = null;
        this.throttleTimer = null;
        this.abortController = null;
        this.athletes = [];
        this.dropdown = document.createElement("ul");
        this.dropdown.className = "combobox-dropdown d-none";
        document.body.appendChild(this.dropdown);
        this.handleOutsideMousedown = (event) => {
            if (
                this.open &&
                event.target !== this.input &&
                !this.dropdown.contains(event.target)
            ) {
                this.close();
            }
        };
        this.reposition = () => this.positionDropdown();
        document.addEventListener("mousedown", this.handleOutsideMousedown);
        input.addEventListener("input", () => this.scheduleSearch());
        input.addEventListener("keydown", (event) => this.handleKeydown(event));
        input.addEventListener("blur", () => {
            this.closeTimer = setTimeout(() => this.close(), 120);
        });
        this.dropdown.addEventListener("mousedown", (event) => {
            const option = event.target.closest(".combobox-option");
            if (!option) {
                return;
            }
            event.preventDefault(); // фокус остаётся в поле до выбора варианта
            // Идентификация строго по индексу: в списке бывают тёзки
            // (два Student с одинаковым ФИО), поиск по имени выбирал бы
            // первого из них.
            const athlete = this.athletes[Number(option.dataset.index)];
            if (athlete) {
                this.select(athlete);
            }
        });
    }

    scheduleSearch() {
        clearTimeout(this.throttleTimer);
        const query = this.input.value.trim();
        if (query.length < 2) {
            this.abortRequest();
            this.close();
            return;
        }
        this.throttleTimer = setTimeout(() => this.search(query), 250);
    }

    abortRequest() {
        if (this.abortController) {
            this.abortController.abort();
            this.abortController = null;
        }
    }

    async search(query) {
        this.abortRequest();
        const controller = new AbortController();
        this.abortController = controller;
        try {
            const response = await fetch(
                `/api/athletes/search?q=${encodeURIComponent(query)}`,
                {signal: controller.signal}
            );
            if (!response.ok) {
                this.close();
                return;
            }
            const athletes = await response.json();
            if (controller.signal.aborted) {
                return;
            }
            let visible = Array.isArray(athletes) ? athletes : [];
            // onlyStudents и includeStudents — взаимоисключающие режимы:
            // сначала проверяем onlyStudents, иначе фильтр «не-карточки»
            // успеет выкинуть карточки до того, как их попросили оставить.
            if (this.onlyStudents) {
                visible = visible.filter((item) => item.kind === "student");
            } else if (!this.includeStudents) {
                visible = visible.filter((item) => item.kind !== "student");
            }
            this.athletes = visible;
            if (this.athletes.length) {
                this.openDropdown();
            } else {
                this.close();
            }
        } catch (error) {
            // Тихо: ошибка сети или отмена — поле ФИО остаётся свободным вводом.
            if (!controller.signal.aborted) {
                this.close();
            }
        }
    }

    isOpen() {
        return this.open;
    }

    openDropdown() {
        this.renderOptions();
        this.open = true;
        this.dropdown.classList.remove("d-none");
        this.positionDropdown();
        window.addEventListener("scroll", this.reposition, true);
        window.addEventListener("resize", this.reposition);
    }

    close() {
        this.open = false;
        this.highlightIndex = -1;
        this.dropdown.classList.add("d-none");
        window.removeEventListener("scroll", this.reposition, true);
        window.removeEventListener("resize", this.reposition);
    }

    renderOptions() {
        this.highlightIndex = -1;
        this.dropdown.replaceChildren(
            ...this.athletes.map((athlete, index) => {
                const item = document.createElement("li");
                item.dataset.index = String(index);
                item.setAttribute("role", "option");
                if (athlete.kind === "student") {
                    return this.renderStudentOption(item, athlete);
                }
                item.className = "combobox-option";
                item.textContent = athlete.name;
                item.title = this.optionMeta(athlete).join(", ");
                return item;
            })
        );
    }

    optionMeta(athlete) {
        const parts = [athlete.institute, athlete.group, athlete.course ? `курс ${athlete.course}` : ""];
        return [athlete.sex, ...parts].filter(Boolean);
    }

    // Вариант-карточка: имя + бейдж «карточка» первой строкой (без номера —
    // карточки видит только admin, прочим ролям число ничего не говорит),
    // мета «институт · группа X · N курс» — второй (только непустые части,
    // white-space normal — длинные подписи переносятся, а не обрезаются).
    renderStudentOption(item, athlete) {
        item.className = "combobox-option combobox-option--student";
        const nameLine = document.createElement("div");
        nameLine.className = "combobox-option__name";
        nameLine.textContent = athlete.name;
        const badge = document.createElement("span");
        badge.className = "badge text-bg-light border ms-1";
        badge.textContent = "карточка";
        nameLine.append(badge);
        const meta = [athlete.institute, athlete.group, athlete.course ? `курс ${athlete.course}` : ""].filter(Boolean);
        item.title = this.optionMeta(athlete).join(", ");
        item.append(nameLine);
        if (meta.length) {
            const metaLine = document.createElement("div");
            metaLine.className = "combobox-option__meta";
            metaLine.textContent = meta.join(" · ");
            item.append(metaLine);
        }
        return item;
    }

    visibleOptions() {
        return Array.from(this.dropdown.querySelectorAll(".combobox-option"));
    }

    updateHighlight() {
        const options = this.visibleOptions();
        options.forEach((item, index) => {
            item.classList.toggle("combobox-option-active", index === this.highlightIndex);
        });
        const active = options[this.highlightIndex];
        if (active) {
            active.scrollIntoView({block: "nearest"});
        }
    }

    positionDropdown() {
        if (!this.open) {
            return;
        }
        const rect = this.input.getBoundingClientRect();
        this.dropdown.style.left = `${rect.left}px`;
        this.dropdown.style.top = `${rect.bottom + 2}px`;
        this.dropdown.style.minWidth = `${rect.width}px`;
    }

    handleKeydown(event) {
        if (!this.open) {
            return;
        }
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            event.stopPropagation();
            const options = this.visibleOptions();
            if (!options.length) {
                return;
            }
            const direction = event.key === "ArrowDown" ? 1 : -1;
            this.highlightIndex = (this.highlightIndex + direction + options.length) % options.length;
            this.updateHighlight();
            return;
        }
        if (event.key === "Enter") {
            // Открытый список перехватывает Enter: выбор подсвеченного или
            // закрытие — сохранение строки не срабатывает.
            event.preventDefault();
            event.stopPropagation();
            if (this.highlightIndex >= 0) {
                const option = this.visibleOptions()[this.highlightIndex];
                const athlete = option ? this.athletes[Number(option.dataset.index)] : null;
                if (athlete) {
                    this.select(athlete);
                    return;
                }
            }
            this.close();
            return;
        }
        if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            this.close();
        }
    }

    select(athlete) {
        this.input.value = athlete.name;
        this.close();
        this.onSelect(athlete);
    }

    // См. InlineComboBox.isOpen: строковый обработчик Enter/Escape в
    // capture-фазе не трогает строку, пока открыт список резолвера.
    isOpen() {
        return this.open;
    }

    destroy() {
        clearTimeout(this.closeTimer);
        clearTimeout(this.throttleTimer);
        this.abortRequest();
        this.close();
        document.removeEventListener("mousedown", this.handleOutsideMousedown);
        this.dropdown.remove();
    }
}

// P2 (Event Model): поля инлайн-правки, принадлежащие событию календаря.
// У строк с data-calendar-event-id они рендерятся заблокированными — значения
// берутся из соревнования (правка — со страницы события), сервер подделку
// формы тоже отвергает. Один и тот же title — «Изменяется в соревновании».
const EVENT_OWNED_EDIT_KEYS = ["sport", "date", "level", "name"];
const EVENT_OWNED_FIELD_TITLE = "Изменяется в соревновании";

// Мульти-фильтры главной: открытие/закрытие меню — собственная механика,
// БЕЗ Bootstrap Dropdown. Бандл bootstrap.min.js не включает Popper, и
// data-api падает в Dropdown.show() → _createPopper(). Триггер (кнопка без
// data-bs-toggle) сам вешает класс .show на меню и синхронизирует
// aria-expanded. Общие слушатели document вешаются РОВНО ОДИН раз:
// initIndexFilterCard вызывается повторно при замене content-wrapper.
let indexMultiDocumentListenersBound = false;

function closeIndexMultiMenu(control) {
    const menu = control.querySelector(".index-multi__menu");
    const toggleButton = control.querySelector(".dropdown-toggle");
    if (menu) {
        menu.classList.remove("show");
    }
    if (toggleButton) {
        toggleButton.setAttribute("aria-expanded", "false");
    }
}

function bindIndexMultiDocumentListeners() {
    if (indexMultiDocumentListenersBound) {
        return;
    }
    indexMultiDocumentListenersBound = true;
    // Клик вне .index-multi закрывает меню; клик внутри (чекбокс/лейбл/
    // шапка «Выбрано: N»/«Сбросить») — нет: фильтр настраивают по одному
    // значению без закрытия. Клик по триггеру соседнего фильтра попадает
    // «вне» остальных — открыто остаётся максимум одно меню.
    document.addEventListener("click", (event) => {
        document.querySelectorAll(".index-multi").forEach((control) => {
            if (!control.contains(event.target)) {
                closeIndexMultiMenu(control);
            }
        });
    });
    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") {
            return;
        }
        document.querySelectorAll(".index-multi").forEach((control) => {
            const menu = control.querySelector(".index-multi__menu");
            if (!menu || !menu.classList.contains("show")) {
                return;
            }
            closeIndexMultiMenu(control);
            const toggleButton = control.querySelector(".dropdown-toggle");
            if (toggleButton) {
                toggleButton.focus();
            }
        });
    });
}

class Main {
    constructor() {
        this.draggedColumnKey = null;
        this.isAthlete = document.body.dataset.role === "athlete";
        this.activeComboBoxes = [];
        this.activeFioResolvers = [];
        // Защита от двойного сабмита инлайн-правки/новой строки (см. save*Edit).
        this.inlineSaving = false;
        this.newRowSaving = false;
        this.initializeDomReferences();
        this.createInstances();
        this.hangEvents();
        this.initTableFeatures();
        this.bindUnsavedInputGuard();
    }

    // Защита несохранённого ввода (UX P0): закрытие/перезагрузка вкладки во
    // время инлайн-правки или ввода новой строки — браузерное подтверждение
    // вместо молчаливой потери. Внутренняя навигация (пагинация, фильтры,
    // drag-drop) уже защищена guard'ами hasActiveRowEdit в своих обработчиках.
    bindUnsavedInputGuard() {
        window.addEventListener("beforeunload", (event) => {
            if (this.hasActiveRowEdit()) {
                event.preventDefault();
                event.returnValue = "";
            }
        });
    }

    initializeDomReferences() {
        this.contentWrapper = document.querySelector(".content-wrapper");
        this.importForm = document.querySelector(".import-form");
        this.profileForm = document.querySelector(".profile-form");
        this.profileFormStatus = document.querySelector(".profile-form__status");
        this.profileNameHint = document.querySelector(".profile-form__name-hint");
        this.profileFormCancelButton = document.querySelector(".profile-form__cancel-button");
        this.addEmptyRowButton = document.querySelector(".add-empty-row-button");
        this.mergeForm = document.querySelector(".merge-form");
        this.mergeControls = document.querySelector(".merge-controls");
        this.mergeApplyButton = document.querySelector(".merge-apply-button");
        this.mergeStatus = document.querySelector(".merge-status");
        // Reports v2: форма условий отчёта (.report-form, только /reports) —
        // GET-навигация; старая механика fetch-обновления /report удалена.
        this.reportForm = document.querySelector(".report-form");
        this.reportExportForm = document.querySelector(".report-export-form");
        this.fileInput = document.querySelector(".import-form__input");
        this.attachmentFileInput = document.querySelector(".attachment-file-input");
        this.importButton = document.querySelector(".import-form__button");
        this.loader = document.querySelector(".loader");
        this.exportButton = document.querySelector(".export-button");
        this.tableLayoutButton = document.querySelector(".table-layout-button");
        this.tableLayoutResetButton = document.querySelector(".table-layout-reset-button");
        this.tableLayoutCloseButton = document.querySelector(".table-layout-close-button");
        this.tableControlsPanel = document.querySelector(".table-controls-panel");
        this.tableColumnsManager = document.querySelector(".table-columns-manager");
        this.dateRangePickerElement = document.querySelector('.datetime');
        this.tableCard = document.querySelector(".table-card");
        this.tableElement = document.querySelector(".interactive-table");
        this.themeToggleButton = document.querySelector(".theme-toggle-button");
        this.hintsToggleButton = document.querySelector(".hints-toggle-button");
        this.usersSearchInput = document.querySelector(".users-search-input");
        this.usersRoleFilter = document.querySelector(".users-role-filter");
        this.usersTable = document.querySelector(".users-table");
    }

    createInstances() {
        if (this.dateRangePickerElement) {
            this.dateRangePicker = new DateRangePicker(this.dateRangePickerElement, {
                format: "dd.mm.yyyy"
            });
        }
        if (document.querySelector(".filter__level")) {
            this.levelSelect = NiceSelect.bind(document.querySelector(".filter__level"), {searchable: true, searchtext: "Найти"});
        }
        if (document.querySelector(".filter__position")) {
            this.positionSelect = NiceSelect.bind(document.querySelector(".filter__position"), {searchable: true, searchtext: "Найти"});
        }
    }

    destroyInstances() {
        if (this.levelSelect) {
            this.levelSelect.destroy();
            this.levelSelect = null;
        }
        if (this.positionSelect) {
            this.positionSelect.destroy();
            this.positionSelect = null;
        }
    }

    hangEvents() {
        if (this.importForm) {
            this.importForm.addEventListener("submit", (event) => this.handleSubmitImportForm(event));
        }
        if (this.fileInput) {
            this.fileInput.addEventListener("change", () => this.setDisabledImportButton(false));
        }
        if (this.reportForm) {
            // Reports v2: GET-навигация — сабмит это переход по ссылке
            // (пустые поля не попадают в URL), никакого fetch-обновления.
            this.reportForm.addEventListener("submit", (event) => this.handleReportFormSubmit(event));
            this.reportForm.addEventListener("input", (event) => {
                if (event.target.name) {
                    this.refreshReportMultiselects();
                    this.updateReportDirtyState();
                }
            });
            this.reportForm.addEventListener("change", (event) => this.handleReportFormChange(event));
        }
        if (this.reportExportForm) {
            this.reportExportForm.addEventListener("submit", (event) => this.handleReportExportFormSubmit(event));
        }
        if (this.exportButton) {
            this.exportButton.addEventListener("click", () => this.handleClickExportButton());
        }
        if (this.tableLayoutButton) {
            this.tableLayoutButton.addEventListener("click", () => this.toggleTableLayoutPanel());
        }
        if (this.tableLayoutCloseButton) {
            this.tableLayoutCloseButton.addEventListener("click", () => this.toggleTableLayoutPanel(false));
        }
        if (this.tableLayoutResetButton) {
            this.tableLayoutResetButton.addEventListener("click", () => this.resetTableLayout());
        }
        if (this.addEmptyRowButton) {
            this.addEmptyRowButton.addEventListener("click", () => this.startNewRowEdit());
        }
        if (this.profileForm) {
            this.profileForm.addEventListener("submit", (event) => this.handleSubmitProfileForm(event));
            this.fetchProfile().then(() => {
                this.fillProfileForm();
                this.checkStudentNameHint(this.loadProfile().student_name);
            });
        } else if (this.isAthlete) {
            // На странице таблицы формы профиля нет, но профиль нужен для
            // автоподстановки в инлайн-строку новой записи (замечание №1:
            // отдельная форма ввода убрана, ввод — только «+ Пустая строка»).
            this.fetchProfile();
        }
        if (this.profileFormCancelButton) {
            this.profileFormCancelButton.addEventListener("click", () => this.resetProfile());
        }
        if (this.mergeForm) {
            this.mergePreview = null;
            this.mergeForm.addEventListener("submit", (event) => this.handleMergePreview(event));
        }
        if (this.mergeApplyButton) {
            this.mergeApplyButton.addEventListener("click", () => this.handleMergeApply());
        }
        if (this.attachmentFileInput) {
            this.attachmentFileInput.addEventListener("change", () => this.handleAttachmentSelected());
        }
        if (this.themeToggleButton) {
            this.updateThemeToggleButton();
            this.themeToggleButton.addEventListener("click", () => this.handleThemeToggle());
        }
        if (this.hintsToggleButton) {
            this.updateHintsToggleButton();
            this.hintsToggleButton.addEventListener("click", () => this.handleHintsToggle());
        }
        this.initPasswordTools();
        this.initUsersPage();
        this.initIndexFilterCard();
        this.initEventDocumentsForm();
        this.initReportPage();
        this.bindContentWrapperEvents();
    }

    // --- Отчёты (Reports v2): GET-навигация /reports. ---
    // Старая механика (fetch /report + замена content-wrapper, чипы-
    // кнопки, лоадер) удалена: применённое состояние = URL, чипы — ссылки.

    initReportPage() {
        if (!this.reportForm) {
            return;
        }
        this.reportInitialEntriesKey = Main.reportEntriesKey(Main.canonicalReportEntries(this.serializeReportForm()));
        this.initReportMultiselects();
        this.updateReportDirtyState();
    }

    // Local checkbox/search actions only change the draft. Native repeated
    // checkbox names are the submitted contract; search has no name.
    initReportMultiselects() {
        const controls = [...this.reportForm.querySelectorAll(".report-multi")];
        const close = (control, restoreFocus = false) => {
            const toggle = control.querySelector(".report-multi__toggle");
            control.querySelector(".report-multi__panel").hidden = true;
            toggle.setAttribute("aria-expanded", "false");
            if (restoreFocus) toggle.focus();
        };
        controls.forEach(control => {
            control.dataset.enhanced = "true";
            close(control);
            const toggle = control.querySelector(".report-multi__toggle");
            const panel = control.querySelector(".report-multi__panel");
            const search = control.querySelector(".report-multi__search");
            toggle.addEventListener("click", () => {
                const open = panel.hidden;
                controls.forEach(other => close(other));
                panel.hidden = !open;
                toggle.setAttribute("aria-expanded", String(open));
                if (open) search.focus();
            });
            search.addEventListener("input", () => this.refreshReportMultiselects());
            search.addEventListener("keydown", event => {
                if (event.key === "Enter") event.preventDefault();
            });
            control.addEventListener("change", event => {
                if (event.target.type === "checkbox") this.refreshReportMultiselects();
            });
            control.querySelector(".report-multi__all").addEventListener("click", () => {
                control.querySelectorAll(".report-multi__option").forEach(option => {
                    if (option.dataset.available !== "false") option.querySelector("input").checked = true;
                });
                this.refreshReportMultiselects();
                this.updateReportDirtyState();
            });
            control.querySelector(".report-multi__clear").addEventListener("click", () => {
                control.querySelectorAll("input[type=checkbox]").forEach(input => { input.checked = false; });
                search.value = "";
                this.refreshReportMultiselects();
                this.updateReportDirtyState();
            });
        });
        this.reportForm.addEventListener("keydown", event => this.handleReportOptionsTab(event));
        document.addEventListener("click", event => {
            controls.forEach(control => { if (!control.contains(event.target)) close(control); });
        });
        document.addEventListener("keydown", event => {
            if (event.key !== "Escape") return;
            controls.forEach(control => {
                if (!control.querySelector(".report-multi__panel").hidden) {
                    event.preventDefault();
                    close(control, true);
                }
            });
        });
        const advanced = this.reportForm.querySelector(".report-filters__advanced");
        advanced?.addEventListener("toggle", () => {
            if (!advanced.open) controls.filter(control => advanced.contains(control)).forEach(control => close(control));
        });
        this.reportForm.querySelectorAll('[name="date_from"], [name="date_to"]').forEach(input => {
            input.addEventListener("changeDate", () => {
                this.refreshReportMultiselects();
                this.updateReportDirtyState();
            });
        });
        this.refreshReportMultiselects();
    }

    handleReportOptionsTab(event) {
        if (event.key !== "Tab") return;
        // Match visual review order without moving named controls (native Back).
        // Keep native Space/checkbox behavior and the surrounding form's Tab order.
        const focusable = [...this.reportForm.querySelectorAll('input, select, button, a[href]')]
            .filter(input => !input.disabled && input.type !== "hidden" && input.getClientRects().length);
        const isEventOption = input => input.name === "event_id" && input.type === "checkbox";
        const eventOptions = focusable.filter(isEventOption).sort((a, b) =>
            Number(a.closest("label").dataset.review === "true") - Number(b.closest("label").dataset.review === "true"));
        let position = 0;
        const order = focusable.map(input => isEventOption(input) ? eventOptions[position++] : input);
        const current = order.indexOf(event.target);
        const next = order[current + (event.shiftKey ? -1 : 1)];
        if (current >= 0 && next && (isEventOption(event.target) || isEventOption(next))) {
            event.preventDefault();
            next.focus();
        }
    }

    // Compare calendar components, never browser/UTC instants or normalized invalid dates.
    static reportDateISO(value) {
        const match = /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/.exec(value.trim());
        if (!match) return null;
        const [, day, month, year] = match.map(Number);
        const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
        const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
        if (year < 1 || month < 1 || month > 12 || day < 1 || day > days[month - 1]) return null;
        return `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    }

    reportLegacyPeriodUnchanged() {
        const applied = new URLSearchParams(this.reportForm.dataset.appliedQuery || "");
        return !applied.has("period") && (applied.has("date_from") || applied.has("date_to"))
            && ["date_from", "date_to"].every(key => this.reportForm.querySelector(`[name="${key}"]`).value === (applied.get(key) || ""));
    }

    reportDraftPeriod() {
        const period = this.reportForm.querySelector(".report__period").value;
        if (period === "all") return {valid: true, start: "", end: ""};
        const year = /^(\d{4})-(\d{4})$/.exec(period);
        if (year && Number(year[1]) >= 1 && Number(year[1]) <= 9998 && Number(year[2]) === Number(year[1]) + 1) {
            return {valid: true, start: `${year[1]}-09-01`, end: `${year[2]}-08-31`};
        }
        if (period !== "custom") return {valid: false};
        const values = ["date_from", "date_to"].map(key => this.reportForm.querySelector(`[name="${key}"]`).value.trim());
        const [start, end] = values.map(value => value ? Main.reportDateISO(value) : "");
        const legacy = this.reportLegacyPeriodUnchanged();
        return {valid: (legacy || Boolean(start && end)) && start !== null && end !== null && (!start || !end || start <= end), start, end};
    }

    refreshReportMultiselects() {
        const period = this.reportDraftPeriod();
        const eventPeriod = period.valid ? period : (this.reportLastValidPeriod || period);
        if (period.valid) this.reportLastValidPeriod = period;
        const institutes = [...this.reportForm.querySelectorAll('[name="institute"]:checked')].map(input => input.value);
        const groupControl = this.reportForm.querySelector('.report-multi[data-filter-key="group"]');
        let groupsByInstitute = {};
        try { groupsByInstitute = JSON.parse(groupControl?.dataset.groupsByInstitute || "{}"); }
        catch (error) { /* Malformed optional narrowing must not discard selected values. */ }
        const allowedGroups = new Set(institutes.flatMap(value => groupsByInstitute[value] || []));
        this.reportForm.querySelectorAll(".report-multi").forEach(control => {
            const checked = [...control.querySelectorAll("input[type=checkbox]:checked")];
            const label = control.dataset.label;
            const selectedLabel = checked.length === 1 ? checked[0].closest("label").querySelector(".report-multi__option-text > span").textContent.trim() : "";
            const summary = !checked.length ? "Все" : checked.length === 1 && selectedLabel.length < 38 ? selectedLabel : `Выбрано: ${checked.length}`;
            control.querySelector(".report-multi__value").textContent = summary;
            const toggle = control.querySelector(".report-multi__toggle");
            toggle.setAttribute("aria-label", `${label}: ${summary}`);
            toggle.title = checked.map(input => input.closest("label").innerText.trim()).join(", ");
            control.querySelector(".report-multi__count").textContent = `Выбрано: ${checked.length}`;
            const query = control.querySelector(".report-multi__search").value.trim().toLocaleLowerCase("ru");
            let shown = 0;
            let reviewCount = 0;
            const isEvent = control.dataset.filterKey === "event_id";
            control.querySelectorAll(".report-multi__option").forEach(option => {
                const input = option.querySelector("input");
                const available = isEvent
                    ? eventPeriod.valid && Boolean(option.dataset.dateFrom)
                        && (!eventPeriod.end || option.dataset.dateFrom <= eventPeriod.end)
                        && (!eventPeriod.start || option.dataset.dateTo >= eventPeriod.start)
                    : control !== groupControl || !institutes.length || allowedGroups.has(input.value);
                option.dataset.available = String(available);
                option.querySelector(".report-multi__outside").hidden = available || !input.checked;
                const matches = option.querySelector(".report-multi__option-text > span").textContent.toLocaleLowerCase("ru").includes(query)
                    || input.value.toLocaleLowerCase("ru").includes(query)
                    || [...option.querySelectorAll("small:not(.report-multi__outside)")].some(item => item.textContent.toLocaleLowerCase("ru").includes(query));
                const review = isEvent && !available && input.checked;
                if (isEvent) {
                    // Native Back restoration pairs repeated names by DOM position.
                    // Keep controls in stable ID order; CSS separates the review section.
                    option.dataset.review = String(review);
                    if (review) reviewCount++;
                }
                option.hidden = review ? false : (!available && !input.checked) || !matches;
                if (!option.hidden && !review) shown++;
            });
            const empty = control.querySelector(".report-multi__empty");
            empty.hidden = shown > 0;
            if (control.querySelector(".report-multi__option")) empty.textContent = query ? "Ничего не найдено" : isEvent ? "За выбранный период соревнований нет" : "Нет доступных значений";
            if (isEvent) {
                control.querySelector(".report-multi__review").hidden = !reviewCount;
                const status = control.querySelector("#report-event-review-summary");
                status.hidden = period.valid && !reviewCount;
                status.textContent = !period.valid
                    ? "Укажите корректные границы периода для обновления списка. Выбор сохранён."
                    : reviewCount ? `Вне периода или недоступно: ${reviewCount}. Снимите эти соревнования или измените период, чтобы применить изменения.` : "";
                this.reportEventReviewCount = reviewCount;
            }
        });
    }

    // Сериализация формы условий: пустые значения отбрасываются (пустое не
    // должно попадать в URL), дефолтный разрез «student» — тоже (сервер
    // считает его отсутствием, см. collect_applied_report_query).
    serializeReportForm(options = {}) {
        const entries = [];
        new FormData(this.reportForm).forEach((value, key) => {
            const text = String(value);
            if (!text) {
                return;
            }
            if (key === "group_by" && (text === "student" || options.excludeSlice)) {
                return;
            }
            entries.push([key, text]);
        });
        return entries;
    }

    static reportEntriesKey(entries) {
        return JSON.stringify([...new Set(entries.map(entry => JSON.stringify(entry)))].sort());
    }

    // Канонизация записей для сравнения с применённым запросом:
    // period=all — как отсутствие period (нет фильтра дат; так
    // восстанавливается форма применённого отчёта без period), а
    // period=custom с датами — как прямые даты (легаси-ссылки без period;
    // см. collect_applied_report_query / build_report_form_values).
    static canonicalReportEntries(entries) {
        const hasDates = entries.some(([key]) => key === "date_from" || key === "date_to");
        return entries.filter(([key, value]) => (
            key !== "period" || (value !== "all" && !(value === "custom" && hasDates))
        ));
    }

    reportFormIsDirty() {
        // Отчёт ещё не применён — предупреждать не о чем.
        if (!this.reportForm.dataset.appliedQuery) {
            return false;
        }
        const applied = Main.canonicalReportEntries(
            Array.from(new URLSearchParams(this.reportForm.dataset.appliedQuery).entries()),
        );
        return Main.reportEntriesKey(applied) !== Main.reportEntriesKey(Main.canonicalReportEntries(this.serializeReportForm()));
    }

    updateReportDirtyState() {
        if (!this.reportForm) {
            return;
        }
        const advanced = this.reportForm.querySelector(".report-filters__advanced");
        if (advanced) {
            const count = [...advanced.querySelectorAll("input[name], select[name]")].filter(input => input.type === "checkbox" ? input.checked : Boolean(input.value)).length;
            const badge = advanced.querySelector(".report-filters__advanced-count");
            badge.textContent = count;
            badge.hidden = !count;
        }
        const dirty = this.reportFormIsDirty();
        const warning = document.querySelector(".report-dirty-warning");
        if (warning) {
            warning.classList.toggle("d-none", !dirty);
        }
        const button = this.reportForm.querySelector(".report-form__submit");
        if (button) {
            button.textContent = dirty ? "Применить изменения" : "Сформировать отчёт";
            button.title = dirty ? "Условия изменены — примените их, чтобы пересчитать отчёт" : "";
        }
    }

    handleReportFormChange(event) {
        // Период ↔ даты: «Произвольный диапазон…» раскрывает поля дат
        // (DateRangePicker уже привязан к .datetime), любой другой выбор —
        // поля скрыты И выключены (иначе даты утекут в URL).
        const periodSelect = this.reportForm.querySelector(".report__period");
        const datesColumn = this.reportForm.querySelector(".report__dates");
        if (event.target === periodSelect && datesColumn) {
            const custom = periodSelect.value === "custom";
            datesColumn.classList.toggle("d-none", !custom);
            this.reportForm.querySelectorAll('[name="date_from"], [name="date_to"]').forEach((input) => {
                input.disabled = !custom;
            });
        }
        this.refreshReportMultiselects();
        // Смена разреза: автоприменение навигацией, если кроме самого
        // разреза форма не «грязная»; иначе — обычное ожидающее изменение.
        if (event.target === this.reportForm.querySelector(".report__group-by")) {
            const appliedQuery = this.reportForm.dataset.appliedQuery;
            if (appliedQuery) {
                const applied = Main.canonicalReportEntries(
                    Array.from(new URLSearchParams(appliedQuery).entries())
                        .filter(([key]) => key !== "group_by"),
                );
                const current = Main.canonicalReportEntries(this.serializeReportForm({excludeSlice: true}));
                if (Main.reportEntriesKey(applied) === Main.reportEntriesKey(current)) {
                    this.navigateReportForm();
                    return;
                }
            }
        }
        this.updateReportDirtyState();
    }

    handleReportFormSubmit(event) {
        event.preventDefault();
        this.navigateReportForm();
    }

    // Навигация по применённому URL: пустые поля и дефолтный разрез
    // выключаются (не отправляются), дальше обычный GET-переход формы.
    // form.submit() намеренно не зовёт обработчик submit — рекурсии нет.
    navigateReportForm() {
        this.refreshReportMultiselects();
        const changed = this.reportFormIsDirty() || this.reportInitialEntriesKey !== Main.reportEntriesKey(Main.canonicalReportEntries(this.serializeReportForm()));
        if (changed && this.reportEventReviewCount) {
            this.updateReportDirtyState();
            const status = this.reportForm.querySelector("#report-event-review-summary");
            status.hidden = false;
            status.textContent = "Снимите соревнования вне периода или недоступные, либо измените период, чтобы применить изменения.";
            this.reportForm.querySelector('[data-filter-key="event_id"] .report-multi__toggle').focus();
            return;
        }
        // Preserve valid single-bound legacy URLs when only another filter/slice changes.
        if (this.reportForm.querySelector(".report__period").value === "custom" && this.reportLegacyPeriodUnchanged()) {
            this.reportForm.querySelector(".report__period").disabled = true;
        }
        this.reportForm.querySelectorAll("input, select").forEach((control) => {
            const isDefaultSlice = control.name === "group_by" && control.value === "student";
            if (!control.value || isDefaultSlice) {
                control.disabled = true;
            }
        });
        this.reportForm.submit();
    }

    // Экспорт строится из применённого запроса скрытыми input (сервер);
    // клиентская проверка — минимум одна колонка (сервер отдаёт 400).
    handleReportExportFormSubmit(event) {
        const selectedColumns = this.reportExportForm.querySelectorAll(".report-export-column:checked");
        if (!selectedColumns.length) {
            event.preventDefault();
            notify({type: "warning", text: "Выберите хотя бы одну колонку"});
        }
    }

    // Кнопки «Сгенерировать»/«Копировать» у полей пароля (.password-generate-
    // button / .password-copy-button, цель — data-password-input): документ-
    // левел обработчик на ЛЮБОЙ странице — эти кнопки есть не только в
    // модалах /admin/users, но и в формах выдачи доступа атлета на карточке
    // студента (где таблицы пользователей нет и initUsersPage выходит
    // раньше привязки). Генерация — криптослучайная, только на клиенте.
    initPasswordTools() {
        document.addEventListener("click", (event) => this.handlePasswordToolsClick(event));
    }

    handlePasswordToolsClick(event) {
        const generateButton = event.target.closest(".password-generate-button");
        if (generateButton) {
            const input = document.querySelector(generateButton.dataset.passwordInput);
            if (input) {
                input.value = this.generatePassword();
                input.focus();
            }
            return;
        }
        const copyButton = event.target.closest(".password-copy-button");
        if (copyButton) {
            const input = document.querySelector(copyButton.dataset.passwordInput);
            if (input && input.value) {
                this.copyPasswordToClipboard(input.value, copyButton);
            }
        }
    }

    // Страница «Пользователи» (/admin/users): живой поиск по логину и фильтр
    // по роли — чистый клиент над уже загруженной таблицей, без запросов к
    // серверу. Модалы действий (пароль/ФИО/удаление) наполняются из
    // data-атрибутов кнопки строки.
    initUsersPage() {
        if (!this.usersTable) {
            return;
        }
        if (this.usersSearchInput) {
            this.usersSearchInput.addEventListener("input", () => this.filterUsersRows());
        }
        if (this.usersRoleFilter) {
            this.usersRoleFilter.addEventListener("change", () => this.filterUsersRows());
        }
        document.addEventListener("click", (event) => this.handleUsersPageClick(event));
        const deleteConfirmInput = document.querySelector(".user-delete-confirm");
        if (deleteConfirmInput) {
            deleteConfirmInput.addEventListener("input", () => this.validateUserDeleteConfirm());
        }
    }

    filterUsersRows() {
        const query = String((this.usersSearchInput && this.usersSearchInput.value) || "").trim().toLowerCase();
        const role = this.usersRoleFilter ? this.usersRoleFilter.value : "";
        this.usersTable.querySelectorAll("tbody tr").forEach((row) => {
            const matchesQuery = !query || (row.dataset.username || "").toLowerCase().includes(query);
            const matchesRole = !role || row.dataset.role === role;
            row.classList.toggle("d-none", !(matchesQuery && matchesRole));
        });
    }

    // Документы соревнования (Event Documents, /calendar/<id>): форма
    // добавления/правки документа. Начальное состояние блока выбранных
    // студентов отрендерено сервером (d-none по режиму доступа правки);
    // здесь только переключение видимости при смене радиокнопки доступа.
    // Сабмит — обычный, без перехвата.
    initEventDocumentsForm() {
        const form = document.querySelector("[data-documents-form]");
        if (!form) {
            return;
        }
        const studentsBlock = form.querySelector("[data-documents-students]");
        const teamBlock = form.querySelector("[data-documents-team]");
        const refreshScope = () => {
            const mode = form.querySelector('input[name="access_mode"]:checked')?.value;
            studentsBlock?.classList.toggle("d-none", mode !== "selected_students");
            teamBlock?.classList.toggle("d-none", mode !== "team_members");
        };
        form.querySelectorAll('input[name="access_mode"]').forEach((radio) => {
            radio.addEventListener("change", refreshScope);
        });
        refreshScope();
    }

    // Карточка фильтров главной (прототип 02): применение и сброс —
    // GET-навигация, фильтры живут в URL (шарость ссылок). «Показывать по»
    // меняет per_page и сбрасывает на первую страницу. Карточка лежит вне
    // content-wrapper и не переинициализируется при обновлении таблицы.
    // Мульти-фильтры (institute/group/sport/level) — dropdown с
    // чекбоксами (механика открытия — наша, см. bindIndexMultiDocumentListeners):
    // счётчики в триггере пересчитываются по фактическому DOM,
    // локальный «Сбросить» не сабмитит; группы сужаются выбранными
    // институтами; ФИО у admin/editor — резолвер атлета (подсказки).
    initIndexFilterCard() {
        const filterCard = document.querySelector(".index-filter-card");
        if (filterCard && filterCard.dataset.bound !== "true") {
            filterCard.dataset.bound = "true";
            const form = filterCard.querySelector("form");
            if (form) {
                form.addEventListener("submit", (event) => {
                    event.preventDefault();
                    const params = new URLSearchParams();
                    new FormData(form).forEach((value, key) => {
                        const text = String(value).trim();
                        if (text) {
                            params.append(key, text);
                        }
                    });
                    const query = params.toString();
                    window.location.href = query ? `/?${query}` : "/";
                });
            }
            const resetLink = filterCard.querySelector(".index-filter-reset");
            if (resetLink) {
                resetLink.addEventListener("click", (event) => {
                    event.preventDefault();
                    window.location.href = "/";
                });
            }
            this.initHybridDateInputs(filterCard);
            this.initIndexMultiSelects(filterCard);
            this.initIndexFioAutocomplete(filterCard);
        }
        const perPageSelect = document.querySelector(".per-page-select");
        if (perPageSelect && perPageSelect.dataset.bound !== "true") {
            perPageSelect.dataset.bound = "true";
            perPageSelect.addEventListener("change", () => {
                const params = new URLSearchParams(perPageSelect.dataset.query || "");
                params.set("per_page", perPageSelect.value);
                window.location.href = `/?${params.toString()}`;
            });
        }
    }

    // Мульти-фильтры главной: клик по триггеру открывает/закрывает меню
    // (класс .show + aria-expanded, без Bootstrap data-api — Popper в бандле
    // нет). Изменение чекбокса пересчитывает подпись
    // триггера («Институт» / «Институт: N»), «Выбрано: N» и видимость шапки
    // меню по фактическому DOM (querySelectorAll по checked), а не счётчиком
    // — серверное состояние и DOM не могут разойтись. Локальный «Сбросить»
    // снимает чекбоксы только своего фильтра и НЕ сабмитит форму.
    initIndexMultiSelects(filterCard) {
        bindIndexMultiDocumentListeners();
        filterCard.querySelectorAll(".index-multi").forEach((control) => {
            if (control.dataset.multiBound === "true") {
                return;
            }
            control.dataset.multiBound = "true";
            const key = control.dataset.filterKey;
            const label = control.dataset.label || key;
            const toggleButton = control.querySelector(".dropdown-toggle");
            const valueSpan = control.querySelector(".index-multi__value");
            const countSpan = control.querySelector(".index-multi__count");
            const clearButton = control.querySelector(".index-multi__clear");
            const updateState = () => {
                const checked = Array.from(control.querySelectorAll("input[type=checkbox]:checked"));
                const count = checked.length;
                if (valueSpan) {
                    valueSpan.textContent = count ? `${label}: ${count}` : label;
                }
                if (toggleButton) {
                    toggleButton.title = checked.map((item) => item.value).join(", ");
                }
                if (countSpan) {
                    countSpan.textContent = `Выбрано: ${count}`;
                    countSpan.classList.toggle("d-none", count === 0);
                }
                if (clearButton) {
                    clearButton.classList.toggle("d-none", count === 0);
                }
            };
            control.addEventListener("change", (event) => {
                if (!event.target.matches("input[type=checkbox]")) {
                    return;
                }
                updateState();
                if (key === "institute") {
                    this.updateIndexGroupNarrowing(filterCard);
                }
            });
            if (toggleButton) {
                toggleButton.addEventListener("click", () => {
                    const menu = control.querySelector(".index-multi__menu");
                    if (!menu) {
                        return;
                    }
                    const willOpen = !menu.classList.contains("show");
                    menu.classList.toggle("show", willOpen);
                    toggleButton.setAttribute("aria-expanded", willOpen ? "true" : "false");
                });
            }
            if (clearButton) {
                clearButton.addEventListener("click", () => {
                    control.querySelectorAll("input[type=checkbox]:checked").forEach((item) => {
                        item.checked = false;
                    });
                    updateState();
                    if (key === "institute") {
                        this.updateIndexGroupNarrowing(filterCard);
                    }
                });
            }
            updateState();
        });
        this.updateIndexGroupNarrowing(filterCard);
    }

    // Сужение групп выбранными институтами (данные — только data-карта
    // «институт → группы» на контроле group). Группа скрывается, только
    // если выбран хотя бы один институт, её data-institutes с ними не
    // пересекаются и она сама не checked; выбранные вне сужения остаются
    // видимыми (text-muted + пояснение). Без выбранных институтов видны
    // все группы; пустой результат — пустышка «Нет групп у выбранных
    // институтов».
    updateIndexGroupNarrowing(filterCard) {
        const groupControl = filterCard.querySelector('.index-multi[data-filter-key="group"]');
        if (!groupControl) {
            return;
        }
        let groupsByInstitute = {};
        try {
            groupsByInstitute = JSON.parse(groupControl.dataset.groupsByInstitute || "{}");
        } catch {
            groupsByInstitute = {};
        }
        const selectedInstitutes = new Set(
            Array.from(
                filterCard.querySelectorAll('.index-multi[data-filter-key="institute"] input[type=checkbox]:checked')
            ).map((item) => item.value)
        );
        const hasInstitutes = selectedInstitutes.size > 0;
        let visibleCount = 0;
        groupControl.querySelectorAll(".index-multi__option").forEach((option) => {
            const checkbox = option.querySelector("input[type=checkbox]");
            const optionInstitutes = String(option.dataset.institutes || "")
                .split(",")
                .map((value) => value.trim())
                .filter(Boolean);
            const intersects = optionInstitutes.some((value) => selectedInstitutes.has(value));
            const checked = Boolean(checkbox && checkbox.checked);
            const hidden = hasInstitutes && !intersects && !checked;
            option.classList.toggle("d-none", hidden);
            if (checked && hasInstitutes && !intersects) {
                option.classList.add("text-muted");
                option.title = "Не входит в выбранные институты";
            } else {
                option.classList.remove("text-muted");
                if (option.title === "Не входит в выбранные институты") {
                    option.removeAttribute("title");
                }
            }
            if (!hidden) {
                visibleCount += 1;
            }
        });
        const narrowEmpty = groupControl.querySelector(".index-multi__narrow-empty");
        if (narrowEmpty) {
            narrowEmpty.classList.toggle("d-none", !(hasInstitutes && visibleCount === 0));
        }
    }

    // Резолвер атлета в поле «ФИО» карточки фильтров: только admin/editor
    // (/api/athletes/search закрыт для остальных ролей). Выбор просто
    // подставляет ФИО — фильтр остаётся свободным вводом (onSelect пуст);
    // режим students: карточки и легаси-имена вместе — реестр знает и тех
    // и других. Отдельный guard: карточка инициализируется повторно.
    initIndexFioAutocomplete(filterCard) {
        const role = document.body.dataset.role;
        if (role !== "admin" && role !== "editor") {
            return;
        }
        const input = filterCard.querySelector("[data-fio-autocomplete]");
        if (!input || input.dataset.fioBound === "true") {
            return;
        }
        input.dataset.fioBound = "true";
        new FioResolver(input, () => {}, {students: true});
    }

    // Гибридные даты (замечание №13) вне инлайн-строки: поля «Дата от/до»
    // карточки фильтров главной. Ручной ввод цифрами с авто-точками плюс
    // datepicker по кнопке-календарю — как в строках таблицы.
    initHybridDateInputs(root) {
        root.querySelectorAll(".index-date-group").forEach((group) => {
            const input = group.querySelector('input[type="text"]');
            const toggleButton = group.querySelector(".index-date-toggle");
            if (!input || input.dataset.dateBound === "true") {
                return;
            }
            input.dataset.dateBound = "true";
            input.addEventListener("input", (event) => this.handleManualDateInput(event));
            input.addEventListener("blur", () => this.normalizeDateInput(input));
            const picker = new Datepicker(input, {autohide: true, format: "dd.mm.yyyy"});
            if (toggleButton) {
                toggleButton.addEventListener("click", (event) => {
                    event.preventDefault();
                    picker.show();
                });
            }
        });
    }

    handleUsersPageClick(event) {
        const passwordButton = event.target.closest(".user-password-button");
        if (passwordButton) {
            this.prepareUserModal(".user-password-form", "/password", passwordButton);
            return;
        }
        const aliasButton = event.target.closest(".user-alias-button");
        if (aliasButton) {
            this.prepareUserModal(".user-alias-form", "/alias", aliasButton);
            return;
        }
        const deleteButton = event.target.closest(".user-delete-button");
        if (deleteButton) {
            this.prepareUserModal(".user-delete-form", "/delete", deleteButton);
            const confirmInput = document.querySelector(".user-delete-confirm");
            if (confirmInput) {
                confirmInput.value = "";
            }
            this.validateUserDeleteConfirm();
            return;
        }
    }

    prepareUserModal(formSelector, actionSuffix, button) {
        const form = document.querySelector(formSelector);
        if (!form) {
            return;
        }
        form.action = `/admin/users/${button.dataset.userId}${actionSuffix}`;
        form.dataset.username = button.dataset.username || "";
        form.querySelectorAll(".user-modal-username").forEach((element) => {
            element.textContent = form.dataset.username;
        });
        const recordsElement = form.querySelector(".user-delete-modal-records-count");
        if (recordsElement) {
            recordsElement.textContent = button.dataset.recordsCount || "0";
        }
    }

    // Кнопка «Удалить» в модале активна только при точном совпадении логина.
    validateUserDeleteConfirm() {
        const form = document.querySelector(".user-delete-form");
        if (!form) {
            return;
        }
        const confirmInput = form.querySelector(".user-delete-confirm");
        const submitButton = form.querySelector(".user-delete-submit");
        const hint = form.querySelector(".user-delete-confirm-hint");
        if (!confirmInput || !submitButton) {
            return;
        }
        const matches = confirmInput.value === form.dataset.username;
        submitButton.disabled = !matches;
        if (hint) {
            hint.classList.toggle("d-none", !confirmInput.value || matches);
        }
    }

    // Криптослучайный пароль: 16 символов из алфавита без двусмысленных
    // (исключены 0, O, 1, l, I). Байты из «хвоста» диапазона отбрасываются,
    // чтобы распределение символов было равномерным без смещения по модулю.
    generatePassword(length = 16) {
        const alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
        if (!window.crypto || !crypto.getRandomValues) {
            notify({type: "error", text: "Браузер не поддерживает безопасную генерацию паролей"});
            return "";
        }
        const maxValidByte = 256 - (256 % alphabet.length);
        const bytes = new Uint8Array(length * 2);
        let password = "";
        while (password.length < length) {
            crypto.getRandomValues(bytes);
            for (const byte of bytes) {
                if (byte >= maxValidByte) {
                    continue;
                }
                password += alphabet[byte % alphabet.length];
                if (password.length === length) {
                    break;
                }
            }
        }
        return password;
    }

    // Копирование пароля: navigator.clipboard, при недоступности —
    // fallback через скрытую textarea и execCommand.
    copyPasswordToClipboard(value, button) {
        const flashCopied = () => {
            const originalLabel = button.textContent;
            button.textContent = "Скопировано";
            setTimeout(() => {
                button.textContent = originalLabel;
            }, 1500);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard
                .writeText(value)
                .then(flashCopied)
                .catch(() => this.copyViaExecCommand(value, flashCopied));
            return;
        }
        this.copyViaExecCommand(value, flashCopied);
    }

    copyViaExecCommand(value, successCallback) {
        const textarea = document.createElement("textarea");
        textarea.value = value;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        let copied = false;
        try {
            copied = document.execCommand("copy");
        } catch {
            copied = false;
        }
        document.body.removeChild(textarea);
        if (copied) {
            successCallback();
        } else {
            notify({type: "error", text: "Не удалось скопировать пароль — скопируйте вручную"});
        }
    }

    // Тема живёт только на клиенте: атрибут data-bs-theme на <html> ставит
    // inline-скрипт в base.html до отрисовки, здесь — переключение и запись
    // выбора в localStorage (ключ "theme": light|dark). Дефолт — светлая.
    handleThemeToggle() {
        const nextTheme = document.documentElement.dataset.bsTheme === "dark" ? "light" : "dark";
        document.documentElement.dataset.bsTheme = nextTheme;
        localStorage.setItem("theme", nextTheme);
        this.updateThemeToggleButton();
    }

    updateThemeToggleButton() {
        if (!this.themeToggleButton) {
            return;
        }
        const isDark = document.documentElement.dataset.bsTheme === "dark";
        this.themeToggleButton.textContent = isDark ? "☀️" : "🌙";
        this.themeToggleButton.title = isDark ? "Включить светлую тему" : "Включить тёмную тему";
    }

    // Подсказки (№26) живут только на клиенте: класс hints-off на <html>
    // ставит inline-скрипт в base.html до отрисовки, здесь — переключение
    // класса и запись выбора в localStorage (ключ "hints": on|off).
    // Дефолт — включены. Скрытие элементов .ui-hint делает main.css.
    handleHintsToggle() {
        const hintsOff = document.documentElement.classList.toggle("hints-off");
        try {
            localStorage.setItem("hints", hintsOff ? "off" : "on");
        } catch (error) {
            // localStorage недоступен — подсказки переключаются до перезагрузки
        }
        this.updateHintsToggleButton();
    }

    updateHintsToggleButton() {
        if (!this.hintsToggleButton) {
            return;
        }
        const hintsOff = document.documentElement.classList.contains("hints-off");
        this.hintsToggleButton.textContent = hintsOff ? "✕💡" : "💡";
        this.hintsToggleButton.title = hintsOff ? "Включить подсказки" : "Скрыть подсказки";
        this.hintsToggleButton.setAttribute("aria-label", hintsOff ? "Показывать подсказки" : "Скрывать подсказки");
    }

    bindContentWrapperEvents() {
        if (!this.contentWrapper || this.contentWrapper.dataset.bound === "true") {
            return;
        }
        this.contentWrapper.dataset.bound = "true";
        this.contentWrapper.addEventListener("click", (event) => this.handleContentWrapperClick(event));
        this.contentWrapper.addEventListener("dblclick", (event) => this.handleContentWrapperDblClick(event));
    }

    getCurrentView() {
        if (!this.tableCard) {
            return "index";
        }
        return this.tableCard.dataset.tableView || "index";
    }

    getTableStateStorageKey() {
        return `competitions-table-state:${this.getCurrentView()}`;
    }

    getDefaultColumnKeys() {
        if (!this.tableElement) {
            return [];
        }
        return Array.from(this.tableElement.querySelectorAll("thead th")).map((header) => header.dataset.columnKey);
    }

    readTableState() {
        const rawState = localStorage.getItem(this.getTableStateStorageKey());
        const defaultOrder = this.getDefaultColumnKeys();
        if (!rawState) {
            return { order: defaultOrder, hidden: [] };
        }
        try {
            const parsed = JSON.parse(rawState);
            const normalizedOrder = [
                ...parsed.order.filter((key) => defaultOrder.includes(key)),
                ...defaultOrder.filter((key) => !parsed.order.includes(key)),
            ];
            const hidden = parsed.hidden.filter((key) => defaultOrder.includes(key));
            return { order: normalizedOrder, hidden };
        } catch {
            return { order: defaultOrder, hidden: [] };
        }
    }

    saveTableState(state) {
        localStorage.setItem(this.getTableStateStorageKey(), JSON.stringify(state));
    }

    setDisabledImportButton(state) {
        if (!this.importButton) {
            return;
        }
        this.importButton.disabled = state;
    }

    cleanFileInput() {
        if (!this.fileInput) {
            return;
        }
        this.setDisabledImportButton(true);
        this.fileInput.value = "";
    }

    toggleTableLayoutPanel(forceState = null) {
        if (!this.tableControlsPanel) {
            return;
        }
        const nextState = forceState === null ? this.tableControlsPanel.classList.contains("d-none") : forceState;
        this.tableControlsPanel.classList.toggle("d-none", !nextState);
    }

    resetTableLayout() {
        if (!this.tableElement) {
            return;
        }
        localStorage.removeItem(this.getTableStateStorageKey());
        this.applyTableState();
    }

    handleClickExportButton() {
        // Несохранённая правка не отменяется молча (UX P0): подтверждение.
        // Отказ — остаёмся в правке, выгрузки не происходит.
        if (this.hasActiveRowEdit()
            && !confirm("В таблице есть несохранённая правка. Выгрузить в Excel и отменить правку?")) {
            return;
        }
        this.exportCurrentTable();
    }

    handleSubmitImportForm(event) {
        event.preventDefault();
        const formData = new FormData(this.importForm);
        this.importFile(formData);
    }

    // Автоформат даты при ручном вводе (замечание №13): только цифры,
    // точки «дд.мм.гггг» проставляются сами — буквы и точки не набираются.
    // Даты-диапазоны (№4): в поле «Дата» строки можно набрать «25-27.06.2026»,
    // «30.01-01.02.2026» или полный «25.06.2026-27.06.2026» — при наличии
    // дефиса авто-точки выключаются, пропускаем цифры, точки и дефис как есть.
    handleManualDateInput(event) {
        const input = event.target;
        if (input.dataset.editKey === "date" && input.value.includes("-")) {
            input.value = input.value.replace(/[^\d.-]/g, "");
            return;
        }
        const digits = input.value.replace(/\D/g, "").slice(0, 8);
        const parts = [];

        if (digits.length > 0) {
            parts.push(digits.slice(0, 2));
        }
        if (digits.length > 2) {
            parts.push(digits.slice(2, 4));
        }
        if (digits.length > 4) {
            parts.push(digits.slice(4, 8));
        }

        input.value = parts.join(".");
    }

    normalizeDateInput(input) {
        // Диапазон («25-27.06.2026» и т.п.) не нормализуем — его форму
        // проверяет сервер.
        if (input.dataset.editKey === "date" && input.value.includes("-")) {
            return;
        }
        const digits = input.value.replace(/\D/g, "").slice(0, 8);
        if (digits.length <= 4) {
            return;
        }

        const day = digits.slice(0, 2);
        const month = digits.slice(2, 4);
        const year = digits.slice(4, 8);
        input.value = [day, month, year].filter(Boolean).join(".");
    }

    // Источник данных профиля — сервер (/api/profile). Профиль хранится
    // в базе и привязывает записи к аккаунту; localStorage больше не нужен.
    loadProfile() {
        return this.cachedProfile || {};
    }

    fetchProfile() {
        return fetch("/api/profile")
            .then((response) => (response.ok ? response.json() : {profile: {}}))
            .then((data) => {
                this.cachedProfile = data.profile || {};
                return this.cachedProfile;
            })
            .catch(() => {
                this.cachedProfile = {};
                return this.cachedProfile;
            });
    }

    saveProfile(profile) {
        this.cachedProfile = profile;
        const formData = new FormData();
        Object.entries(profile).forEach(([key, value]) => formData.append(key, value));
        formData.append("csrf_token", this.getCsrfToken());
        return fetch("/api/profile", {method: "POST", body: formData})
            .then((response) => {
                if (!response.ok) {
                    return response.text().then((message) => {
                        throw new Error(message || "Ошибка сохранения профиля");
                    });
                }
                return response.json();
            })
            .then((data) => {
                this.cachedProfile = data.profile || profile;
                return this.cachedProfile;
            });
    }

    clearProfile() {
        this.cachedProfile = {};
        return this.saveProfile({});
    }

    handleSubmitProfileForm(event) {
        event.preventDefault();
        const formData = new FormData(this.profileForm);
        const profile = {};
        ["student_name", "student_sex", "institute", "group"].forEach((key) => {
            profile[key] = String(formData.get(key) || "").trim();
        });
        this.saveProfile(profile)
            .then(() => {
                this.fillProfileForm();
                if (this.profileFormStatus) {
                    this.profileFormStatus.textContent = "Профиль сохранён";
                }
                this.checkStudentNameHint(profile.student_name);
            })
            .catch((error) => {
                if (this.profileFormStatus) {
                    this.profileFormStatus.textContent = error.message;
                }
            });
    }

    fillProfileForm() {
        if (!this.profileForm) {
            return;
        }
        const profile = this.loadProfile();
        const educationOutput = this.profileForm.querySelector("#profile-education");
        if (educationOutput) {
            educationOutput.textContent = `Год поступления: ${profile.admission_year || "—"}. Текущий курс: ${profile.course || "—"}. Рассчитываются по группе после сохранения.`;
        }
        this.profileForm.querySelectorAll("input, select").forEach((input) => {
            const value = profile[input.name];
            if (value !== undefined) {
                input.value = value;
            }
        });
    }

    resetProfile() {
        if (!this.profileForm) {
            return;
        }
        this.clearProfile().then(() => this.fillProfileForm());
        this.profileForm.reset();
        if (this.profileFormStatus) {
            this.profileFormStatus.textContent = "";
        }
        this.checkStudentNameHint("");
    }

    // Тихая проверка ФИО атлета: Lookup возвращает только факт точного
    // совпадения, чужие ФИО атлету не раскрываются. При любой ошибке сети
    // подсказка не показывается.
    checkStudentNameHint(name) {
        if (!this.isAthlete || !this.profileNameHint) {
            return;
        }
        const trimmed = String(name || "").trim();
        if (!trimmed) {
            this.profileNameHint.textContent = "";
            this.profileNameHint.classList.remove("text-success", "text-warning");
            return;
        }
        fetch(`/api/students/lookup?name=${encodeURIComponent(trimmed)}`)
            .then((response) => (response.ok ? response.json() : Promise.reject(new Error("lookup failed"))))
            .then((data) => {
                if (data.found) {
                    this.profileNameHint.textContent =
                        "Найдены записи с таким ФИО — участия привяжутся к кабинету";
                    this.profileNameHint.className = "profile-form__name-hint form-text mt-1 text-success";
                } else {
                    this.profileNameHint.textContent =
                        "Записей с таким ФИО не найдено — проверьте написание (например, полное ФИО вместо инициалов)";
                    this.profileNameHint.className = "profile-form__name-hint form-text mt-1 text-warning";
                }
            })
            .catch(() => {
                this.profileNameHint.textContent = "";
                this.profileNameHint.classList.remove("text-success", "text-warning");
            });
    }

    // Подсказки ФИО в админке: datalist наполняется один раз за сессию,
    // повторно — после перерисовки контента. Список чужих ФИО доступен
    // только admin/editor; при 403 или ошибке сети поля просто без подсказок.
    renderStudentNamesList() {
        const datalist = document.querySelector("#student-names-list");
        if (!datalist) {
            return;
        }
        if (this.studentNames) {
            datalist.replaceChildren(...this.studentNames.map((name) => {
                const option = document.createElement("option");
                option.value = name;
                return option;
            }));
            return;
        }
        fetch("/api/students")
            .then((response) => (response.ok ? response.json() : Promise.reject(new Error("students list failed"))))
            .then((names) => {
                if (!Array.isArray(names)) {
                    return;
                }
                this.studentNames = names;
                this.renderStudentNamesList();
            })
            .catch(() => {});
    }

    // Автоподстановка из профиля (docs/data-model-decisions.md): профиль
    // подставляет значения в новые записи атлета, пока поле не заполнено;
    // введённое вручную всегда приоритетнее. Используется инлайн-строкой
    // «+ Пустая строка».
    getProfileFieldDefaults() {
        if (!this.isAthlete) {
            return {};
        }
        const profile = this.loadProfile();
        const defaults = {};
        ["student_name", "student_sex", "institute", "group"].forEach((key) => {
            const value = String(profile[key] ?? "").trim();
            if (value) {
                defaults[key] = value;
            }
        });
        return defaults;
    }

    // Формирует form-encoded тело для merge-запросов. CSRF-токен
    // прикрепляется в makeRequest ко всем POST (FormData, URLSearchParams
    // и запросам без тела), здесь дублирование исключено проверкой has().
    buildMergeRequestBody(extraFields = {}) {
        const body = new URLSearchParams(extraFields);
        body.append("csrf_token", this.getCsrfToken());
        return body;
    }

    setMergeStatus(message, tone = "muted") {
        if (!this.mergeStatus) {
            return;
        }
        this.mergeStatus.textContent = message;
        this.mergeStatus.className = `merge-status text-${tone}`;
    }

    resetMergeControls(message, tone) {
        this.mergePreview = null;
        if (this.mergeControls) {
            this.mergeControls.classList.add("d-none");
        }
        if (this.mergeApplyButton) {
            this.mergeApplyButton.disabled = true;
        }
        if (message !== undefined) {
            this.setMergeStatus(message, tone);
        }
    }

    handleMergePreview(event) {
        event.preventDefault();
        this.resetMergeControls();
        const fromName = this.mergeForm.querySelector('[name="from_name"]').value.trim();
        const toName = this.mergeForm.querySelector('[name="to_name"]').value.trim();
        if (!fromName || !toName) {
            this.setMergeStatus("Укажите оба ФИО", "danger");
            return;
        }

        this.makeRequest({
            url: "/admin/students/merge",
            options: {
                method: "POST",
                body: this.buildMergeRequestBody({from_name: fromName, to_name: toName}),
            },
            onSuccess: (responseBody) => {
                let preview;
                try {
                    preview = JSON.parse(responseBody);
                } catch {
                    this.setMergeStatus("Неожиданный ответ сервера", "danger");
                    return;
                }
                const count = preview.records_to_merge;
                if (!count) {
                    this.setMergeStatus("Записей с таким ФИО не найдено", "warning");
                    return;
                }
                this.mergePreview = {fromName, toName, count};
                this.setMergeStatus(`Найдено записей для объединения: ${count}`, "success");
                if (this.mergeControls) {
                    this.mergeControls.classList.remove("d-none");
                }
                if (this.mergeApplyButton) {
                    this.mergeApplyButton.disabled = false;
                }
            },
            onError: (message) => this.setMergeStatus(message || "Ошибка предпросмотра", "danger"),
        });
    }

    handleMergeApply() {
        if (!this.mergePreview || !this.mergeApplyButton || this.mergeApplyButton.disabled) {
            return;
        }
        const {fromName, toName, count} = this.mergePreview;
        const confirmed = confirm(
            `Объединить ${count} записей с «${fromName}» на «${toName}»? Действие необратимо`
        );
        if (!confirmed) {
            return;
        }

        const fields = {
            from_name: fromName,
            to_name: toName,
            confirm: "on",
        };
        const rewriteCheckbox = this.mergeForm.querySelector('[name="rewrite_names"]');
        if (rewriteCheckbox && rewriteCheckbox.checked) {
            fields.rewrite_names = "on";
        }

        this.mergeApplyButton.disabled = true;
        this.makeRequest({
            url: "/admin/students/merge",
            options: {
                method: "POST",
                body: this.buildMergeRequestBody(fields),
            },
            onSuccess: (responseBody) => {
                let result;
                try {
                    result = JSON.parse(responseBody);
                } catch {
                    result = {};
                }
                const merged = typeof result.merged === "number" ? result.merged : count;
                this.resetMergeControls(
                    `Объединено записей: ${merged}` +
                        (result.rewritten_names ? ". Отображаемые ФИО в записях переписаны" : ""),
                    "success"
                );
            },
            onError: (message) => {
                this.mergeApplyButton.disabled = false;
                this.setMergeStatus(message || "Ошибка объединения", "danger");
            },
        });
    }

    getTableLevels() {
        try {
            return JSON.parse(this.tableElement?.dataset.levels || "[]");
        } catch {
            return [];
        }
    }

    // Типы кастомных полей (замечание №1): раньше читались из инпутов
    // удалённой формы ввода, теперь — из data-field-type заголовков колонок
    // таблицы (index.html). Нужны инлайн-строке: тип поля ввода и префикс
    // custom__<key> при сохранении.
    getCustomFieldTypes() {
        const types = {};
        if (!this.tableElement) {
            return types;
        }
        this.tableElement.querySelectorAll("thead th[data-field-type]").forEach((header) => {
            types[header.dataset.columnKey] = header.dataset.fieldType || "text";
        });
        return types;
    }

    // Источники данных комбобоксов (№4/№15): институты — активный datalist
    // страницы, группы — карта институт → группы на таблице (только активные).
    getInstituteOptions() {
        const datalist = document.querySelector("#institute-options");
        if (!datalist) {
            return [];
        }
        return Array.from(datalist.querySelectorAll("option"))
            .map((option) => option.value)
            .filter(Boolean);
    }

    getGroupOptionsByInstitute() {
        try {
            return JSON.parse(this.tableElement?.dataset.groupsByInstitute || "{}");
        } catch {
            return {};
        }
    }

    getAllGroupOptions() {
        const map = this.getGroupOptionsByInstitute();
        return Array.from(new Set(Object.values(map).flat()))
            .sort((first, second) => first.localeCompare(second, "ru"));
    }

    getGroupOptionsForInstitute(instituteValue) {
        const map = this.getGroupOptionsByInstitute();
        const key = String(instituteValue || "").trim();
        if (key && Object.prototype.hasOwnProperty.call(map, key)) {
            return map[key].slice();
        }
        // Институт пуст или ещё не из справочника — подсказываем все группы
        // (свободный ввод остаётся, значение в поле не стирается).
        return this.getAllGroupOptions();
    }

    getComboboxOptions(key, input) {
        if (key === "institute") {
            return this.getInstituteOptions();
        }
        const row = input.closest("tr");
        const instituteInput = row ? row.querySelector('[data-edit-key="institute"]') : null;
        return this.getGroupOptionsForInstitute(instituteInput ? instituteInput.value : "");
    }

    destroyRowComboBoxes() {
        this.activeComboBoxes.forEach((combo) => combo.destroy());
        this.activeComboBoxes = [];
    }

    // Резолвер атлета (№23доп): навешивается на поле ФИО инлайн-строки у
    // ролей с правом записи (атлету — нет: чужие ФИО не запрашиваем, его
    // автоподстановка из собственного профиля уже работает).
    attachFioResolver(row) {
        if (this.isAthlete) {
            return;
        }
        const input = row.querySelector('[data-edit-key="student_name"]');
        if (!input || input.type !== "text") {
            return;
        }
        this.activeFioResolvers.push(new FioResolver(input, (athlete) => {
            this.applyAthleteDefaults(row, athlete);
        }));
    }

    destroyFioResolvers() {
        this.activeFioResolvers.forEach((resolver) => resolver.destroy());
        this.activeFioResolvers = [];
    }

    // Выбор варианта подставляет известные поля в строку и оставляет их
    // редактируемыми: автоподстановка, не принуждение (историчность —
    // docs/data-model-decisions.md). Синтетический input обновляет список
    // групп при подстановке института.
    applyAthleteDefaults(row, athlete) {
        const mapping = {sex: "student_sex", institute: "institute", group: "group"};
        Object.entries(mapping).forEach(([athleteKey, editKey]) => {
            const value = athlete[athleteKey];
            if (!value) {
                return;
            }
            const input = row.querySelector(`[data-edit-key="${editKey}"]`);
            if (input && String(input.value) !== String(value)) {
                input.value = value;
                input.dispatchEvent(new Event("input", {bubbles: true}));
            }
        });
    }

    // Связка институт→группа (№15): смена института перезаполняет открытый
    // список групп; введённая группа сохраняется как есть.
    linkRowCombos(row) {
        const instituteInput = row.querySelector('[data-edit-key="institute"]');
        const groupInput = row.querySelector('[data-edit-key="group"]');
        if (!instituteInput || !groupInput) {
            return;
        }
        instituteInput.addEventListener("input", () => {
            const combo = this.activeComboBoxes.find((item) => item.input === groupInput);
            if (combo) {
                combo.refresh();
            }
        });
    }

    // eventOwned — строка связана со соревнованием (data-calendar-event-id):
    // event-owned поля рендерятся заблокированными текстовыми инпутами без
    // select/datalist/datepicker-обёрток. data-edit-key и классы прежние —
    // FormData собирает и заблокированные значения, они уходят на сервер
    // неизменными (сервер всё равно берёт их из записи-события).
    createInlineInput(key, value, fieldTypes, eventOwned = false) {
        if (key === "course") {
            const output = document.createElement("span");
            output.className = "text-muted small";
            output.textContent = "Рассчитывается по группе и дате";
            return output;
        }
        const readOnly = eventOwned && EVENT_OWNED_EDIT_KEYS.includes(key);
        let input;
        if (readOnly) {
            input = document.createElement("input");
            input.type = "text";
            input.disabled = true;
            input.title = EVENT_OWNED_FIELD_TITLE;
            input.value = value ?? "";
        } else if (key === "student_sex" || key === "level") {
            input = document.createElement("select");
            const options = key === "student_sex"
                ? ["М", "Ж"]
                : this.getTableLevels();
            if (key === "level") {
                options.unshift("");
                if (value && !options.includes(value)) options.push(value);
            }
            options.forEach((optionValue) => {
                const option = document.createElement("option");
                option.value = optionValue;
                option.textContent = optionValue || "Не указан";
                input.append(option);
            });
            input.value = value;
        } else {
            input = document.createElement("input");
            const isCustom = Object.prototype.hasOwnProperty.call(fieldTypes, key);
            const fieldType = isCustom ? fieldTypes[key] : null;
            if (key === "position") {
                input.type = "text";
                input.placeholder = "Место или DNF";
            } else if (key === "course" || fieldType === "number") {
                input.type = "number";
                input.step = "1";
            } else if (fieldType === "url") {
                input.type = "url";
                input.placeholder = "https://";
            } else {
                input.type = "text";
            }
            if (key === "date" || fieldType === "date") {
                input.placeholder = "дд.мм.гггг";
                input.inputMode = "numeric";
                if (key === "date") {
                    input.title = "Диапазон: 25-27.06.2026 или 25.06.2026-27.06.2026";
                }
            }
            // Подсказки справочников: datalist лежит в разметке страницы,
            // id совпадает с ключом поля. Институт и группа — комбобоксы
            // (№4): список групп фильтруется выбранным институтом (№15).
            if (key === "sport") {
                input.setAttribute("list", "sport-options");
            }
            input.value = value ?? "";
        }
        input.dataset.editKey = key;
        input.className = "form-control form-control-sm";
        if (key === "level" && !readOnly) {
            const strict = enhanceLevelSelect(input);
            this.activeComboBoxes.push(strict.combo);
            return strict.wrapper;
        }
        // Комбобокс (№4): поле + кнопка-стрелка + выпадающий список с
        // фильтрацией по подстроке; свободный ввод не ограничен.
        if (input.type === "text" && (key === "institute" || key === "group")) {
            const wrapper = document.createElement("div");
            wrapper.className = "input-group inline-combobox-group";
            const toggleButton = document.createElement("button");
            toggleButton.type = "button";
            toggleButton.className = "btn btn-outline-secondary btn-sm combobox-toggle";
            toggleButton.textContent = "▾";
            toggleButton.title = "Показать список";
            toggleButton.setAttribute("aria-label", "Показать список");
            wrapper.append(input, toggleButton);
            this.activeComboBoxes.push(
                new InlineComboBox(input, toggleButton, () => this.getComboboxOptions(key, input))
            );
            return wrapper;
        }
        // Гибридный ввод даты (замечание №13): поле + иконка-календарь,
        // открывающая datepicker; сам пикер и авто-точки вешаются
        // attachRowDatePickers при сборке строки. Event-owned дата
        // заблокирована — обёртка с календарём ей не нужна.
        if (!input.disabled && input.type === "text" && (key === "date" || fieldTypes[key] === "date")) {
            const wrapper = document.createElement("div");
            wrapper.className = "input-group inline-date-group";
            const toggleButton = document.createElement("button");
            toggleButton.type = "button";
            toggleButton.className = "btn btn-outline-secondary btn-sm inline-date-toggle";
            toggleButton.textContent = "📅";
            toggleButton.title = "Выбрать дату из календаря";
            toggleButton.setAttribute("aria-label", "Выбрать дату из календаря");
            wrapper.append(input, toggleButton);
            return wrapper;
        }
        return input;
    }

    // Поля дат в инлайн-строке (замечание №13): ручной ввод цифрами с
    // авто-точками (handleManualDateInput) + datepicker (fengyuanchen, тот
    // же, что в фильтрах отчёта) с кнопкой-календарем. Выбор из календаря
    // проставляет дату в формате дд.мм.гггг. Возвращает созданные пикеры —
    // их нужно разрушить при отмене правки (иначе «висячие» слушатели).
    // Заблокированные event-owned даты пропускаются.
    attachRowDatePickers(row) {
        const fieldTypes = this.getCustomFieldTypes();
        const pickers = [];
        row.querySelectorAll("[data-edit-key]").forEach((input) => {
            const key = input.dataset.editKey;
            if (key !== "date" && fieldTypes[key] !== "date") {
                return;
            }
            if (input.disabled) {
                return;
            }
            input.addEventListener("input", (event) => this.handleManualDateInput(event));
            input.addEventListener("blur", () => this.normalizeDateInput(input));
            const picker = new Datepicker(input, {
                autohide: true,
                format: "dd.mm.yyyy"
            });
            const toggleButton = input.closest(".inline-date-group")?.querySelector(".inline-date-toggle");
            if (toggleButton) {
                toggleButton.addEventListener("click", (event) => {
                    event.preventDefault();
                    picker.show();
                });
            }
            pickers.push(picker);
        });
        return pickers;
    }

    destroyRowDatePickers(pickers) {
        (pickers || []).forEach((picker) => picker.destroy());
    }

    startInlineEdit(button) {
        const row = button.closest("tr");
        if (!row) {
            return;
        }
        this.cancelInlineEdit();
        this.cancelNewRowEdit();

        const dataset = button.dataset;
        const extraData = dataset.extraJson ? JSON.parse(dataset.extraJson) : {};
        const fieldTypes = this.getCustomFieldTypes();
        // P2: строка связана со соревнованием — event-owned поля строки
        // рендерятся заблокированными (см. createInlineInput).
        const eventOwned = Boolean(dataset.calendarEventId);
        const values = {
            student_name: dataset.studentName,
            student_sex: dataset.studentSex,
            institute: dataset.institute,
            group: dataset.group,
            sport: dataset.sport,
            date: dataset.date,
            level: dataset.level,
            name: dataset.name,
            position: dataset.position,
            course: dataset.course,
        };

        this.inlineEditRow = row;
        this.inlineEditRecordId = dataset.recordId;
        this.inlineEditBackup = row.innerHTML;
        row.classList.add("row-editing");

        Array.from(row.children).forEach((cell) => {
            const key = cell.dataset.columnKey;
            if (!key || key === "index") {
                return;
            }
            if (key === "actions") {
                cell.textContent = "";
                const saveButton = document.createElement("button");
                saveButton.type = "button";
                saveButton.className = "btn btn-sm btn-success inline-save-button";
                saveButton.textContent = "✓";
                saveButton.title = "Сохранить";
                const cancelButton = document.createElement("button");
                cancelButton.type = "button";
                cancelButton.className = "btn btn-sm btn-outline-secondary inline-cancel-button";
                cancelButton.textContent = "✗";
                cancelButton.title = "Отмена";
                cell.append(saveButton, cancelButton);
                return;
            }
            const value = Object.prototype.hasOwnProperty.call(values, key)
                ? values[key]
                : extraData[key] ?? "";
            cell.textContent = "";
            cell.append(this.createInlineInput(key, value, fieldTypes, eventOwned));
        });

        this.inlineDatePickers = this.attachRowDatePickers(row);
        this.linkRowCombos(row);
        this.attachFioResolver(row);
        this.inlineKeydownHandler = (event) => {
            if (this.hasOpenRowDropdown()) {
                return;
            }
            if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                this.cancelInlineEdit();
            } else if (event.key === "Enter" && event.target.tagName !== "SELECT") {
                event.preventDefault();
                event.stopPropagation();
                this.saveInlineEdit();
            }
        };
        // Capture-фаза: обработчик строки срабатывает ДО виджетов поля
        // (datepicker перехватывает Esc, комбобоксы/резолвер — Enter/Esc в
        // target-фазе), иначе Enter/Escape до строки не доходили вовсе.
        row.addEventListener("keydown", this.inlineKeydownHandler, true);
        // Фокус — первое РЕДАКТИРУЕМОЕ поле: event-owned заблокированы.
        const firstInput = row.querySelector("[data-edit-key]:not([disabled])");
        if (firstInput) {
            firstInput.focus();
        }
    }

    saveInlineEdit() {
        if (!this.inlineEditRow || !this.inlineEditRecordId || this.inlineSaving) {
            return;
        }
        const fieldTypes = this.getCustomFieldTypes();
        const formData = new FormData();
        this.inlineEditRow.querySelectorAll("[data-edit-key]").forEach((input) => {
            const key = input.dataset.editKey;
            const name = Object.prototype.hasOwnProperty.call(fieldTypes, key)
                ? `custom__${key}`
                : key;
            formData.append(name, input.value);
        });

        // Защита от повторного Enter и двойного клика по «✓»: один запрос
        // на одно сохранение, флаг снимается в onSuccess/onError.
        this.inlineSaving = true;
        this.makeRequest({
            url: `/competition/${this.inlineEditRecordId}`,
            options: {
                method: "POST",
                body: formData,
            },
            onSuccess: () => {
                this.inlineSaving = false;
                notify({type: "success", text: "Запись успешно обновлена"});
                this.refreshCurrentContent();
            },
            onError: (message) => {
                this.inlineSaving = false;
                notify({type: "error", text: message || "Ошибка сохранения записи"});
            }
        });
    }

    cancelInlineEdit() {
        if (!this.inlineEditRow) {
            return;
        }
        const row = this.inlineEditRow;
        if (this.inlineKeydownHandler) {
            row.removeEventListener("keydown", this.inlineKeydownHandler, true);
            this.inlineKeydownHandler = null;
        }
        this.destroyRowDatePickers(this.inlineDatePickers);
        this.inlineDatePickers = null;
        this.destroyRowComboBoxes();
        this.destroyFioResolvers();
        if (row.isConnected) {
            row.innerHTML = this.inlineEditBackup;
            row.classList.remove("row-editing");
        }
        this.inlineEditRow = null;
        this.inlineEditRecordId = null;
        this.inlineEditBackup = null;
    }

    hasActiveRowEdit() {
        return Boolean(this.inlineEditRow || this.newEditRow);
    }

    // Открытый выпадающий список (комбобокс или резолвер ФИО) работает в два
    // шага: Enter выбирает вариант, Esc закрывает список. Пока список открыт,
    // строковый обработчик Enter/Escape строку не сохраняет и не отменяет.
    hasOpenRowDropdown() {
        return (
            this.activeComboBoxes.some((combo) => combo.isOpen()) ||
            this.activeFioResolvers.some((resolver) => resolver.isOpen())
        );
    }

    buildRowActionsCell(cell, saveClassName) {
        const saveButton = document.createElement("button");
        saveButton.type = "button";
        saveButton.className = `btn btn-sm btn-success ${saveClassName}`;
        saveButton.textContent = "✓";
        saveButton.title = "Сохранить";
        const cancelButton = document.createElement("button");
        cancelButton.type = "button";
        cancelButton.className = "btn btn-sm btn-outline-secondary new-row-cancel-button";
        cancelButton.textContent = "✗";
        cancelButton.title = "Отмена";
        cell.append(saveButton, cancelButton);
    }

    bindRowKeyboardNavigation(row, saveAction, cancelAction) {
        const handler = (event) => {
            if (this.hasOpenRowDropdown()) {
                return;
            }
            if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                cancelAction();
            } else if (event.key === "Enter" && event.target.tagName !== "SELECT") {
                event.preventDefault();
                event.stopPropagation();
                saveAction();
            }
        };
        // Capture-фаза — та же причина, что у инлайн-правки (см. startInlineEdit).
        row.addEventListener("keydown", handler, true);
        return handler;
    }

    startNewRowEdit() {
        if (!this.tableElement) {
            notify({type: "error", text: "Таблица недоступна: нет данных"});
            return;
        }
        if (this.hasActiveRowEdit()) {
            return;
        }
        const tbody = this.tableElement.querySelector("tbody");
        if (!tbody) {
            return;
        }

        const fieldTypes = this.getCustomFieldTypes();
        const profileDefaults = this.getProfileFieldDefaults();
        const row = document.createElement("tr");
        row.classList.add("row-editing", "row-new");

        Array.from(this.tableElement.querySelectorAll("thead th")).forEach((header) => {
            const key = header.dataset.columnKey;
            const cell = document.createElement("td");
            if (key) {
                cell.dataset.columnKey = key;
            }
            // №24 (hotfix): колонка привязанного link-поля скрыта через
            // d-none на th — ячейка инлайн-строки должна скрываться так же,
            // иначе лишнее видимое поле ломает вёрстку строки.
            if (header.classList.contains("d-none")) {
                cell.classList.add("d-none");
            }
            if (!key || key === "index" || key === "status" || key === "attachments") {
                row.append(cell);
                return;
            }
            if (key === "actions") {
                this.buildRowActionsCell(cell, "new-row-save-button");
                row.append(cell);
                return;
            }
            cell.append(this.createInlineInput(key, profileDefaults[key] ?? "", fieldTypes));
            row.append(cell);
        });

        // Новая строка открывается первой строкой таблицы, сразу после
        // заголовков, — до неё не нужно прокручивать сотню записей.
        tbody.prepend(row);
        this.newEditRow = row;

        this.newRowDatePickers = this.attachRowDatePickers(row);
        this.linkRowCombos(row);
        this.attachFioResolver(row);
        this.newRowKeydownHandler = this.bindRowKeyboardNavigation(
            row,
            () => this.saveNewRowEdit(),
            () => this.cancelNewRowEdit()
        );
        row.scrollIntoView({behavior: "smooth", block: "center"});
        // Фокус на первое незаполненное поле (при автоподстановке из профиля
        // заполненные пропускаем); preventScroll, чтобы не сбивать плавную
        // прокрутку к строке.
        const inputs = Array.from(row.querySelectorAll("[data-edit-key]"));
        (inputs.find((input) => !input.value) || inputs[0])?.focus({preventScroll: true});
    }

    saveNewRowEdit() {
        if (!this.newEditRow || this.newRowSaving) {
            return;
        }
        const fieldTypes = this.getCustomFieldTypes();
        const formData = new FormData();
        this.newEditRow.querySelectorAll("[data-edit-key]").forEach((input) => {
            const key = input.dataset.editKey;
            const name = Object.prototype.hasOwnProperty.call(fieldTypes, key)
                ? `custom__${key}`
                : key;
            formData.append(name, input.value);
        });

        // Защита от повторного Enter и двойного клика по «✓» (см. saveInlineEdit).
        this.newRowSaving = true;
        this.makeRequest({
            url: "/competition",
            options: {
                method: "POST",
                body: formData,
            },
            onSuccess: () => {
                this.newRowSaving = false;
                notify({type: "success", text: "Запись успешно добавлена"});
                this.refreshCurrentContent();
            },
            onError: (message) => {
                this.newRowSaving = false;
                notify({type: "error", text: message || "Ошибка добавления записи"});
            }
        });
    }

    cancelNewRowEdit() {
        if (!this.newEditRow) {
            return;
        }
        const row = this.newEditRow;
        if (this.newRowKeydownHandler) {
            row.removeEventListener("keydown", this.newRowKeydownHandler, true);
            this.newRowKeydownHandler = null;
        }
        this.destroyRowDatePickers(this.newRowDatePickers);
        this.newRowDatePickers = null;
        this.destroyRowComboBoxes();
        this.destroyFioResolvers();
        row.remove();
        this.newEditRow = null;
    }

    uploadAttachment(recordId, file) {
        const formData = new FormData();
        formData.append("file", file);
        this.makeRequest({
            url: `/competition/${recordId}/attachments`,
            options: {method: "POST", body: formData},
            onSuccess: (body) => {
                notify({type: "success", text: body || "Файл загружен"});
                this.refreshCurrentContent();
            },
            onError: (message) => notify({type: "error", text: message || "Ошибка загрузки файла"})
        });
    }

    handleAttachmentSelected() {
        const input = this.attachmentFileInput;
        const file = input && input.files && input.files[0];
        const recordId = input && input.dataset.recordId;
        input.value = "";
        if (!file || !recordId) {
            return;
        }
        this.uploadAttachment(recordId, file);
    }

    deleteAttachment(attachmentId, filename) {
        const message = filename
            ? `Удалить вложение «${filename}»?`
            : "Удалить вложение?";
        if (!confirm(message)) {
            return;
        }
        this.makeRequest({
            url: `/attachment/${attachmentId}/delete`,
            options: {method: "POST"},
            onSuccess: () => {
                notify({type: "success", text: "Вложение удалено"});
                this.refreshCurrentContent();
            },
            onError: (message2) => notify({type: "error", text: message2 || "Ошибка удаления вложения"})
        });
    }

    reviewCompetition(recordId, decision) {
        if (decision === "reject") {
            const comment = prompt("Комментарий для владельца записи (необязательно):") ?? "";
            this.makeRequest({
                url: `/competition/${recordId}/review/reject`,
                options: {
                    method: "POST",
                    body: new URLSearchParams({comment}),
                },
                onSuccess: () => {
                    notify({type: "success", text: "Запись отклонена"});
                    this.refreshCurrentContent();
                },
                onError: (message) => notify({type: "error", text: message || "Ошибка отклонения записи"})
            });
            return;
        }
        this.makeRequest({
            url: `/competition/${recordId}/review/approve`,
            options: {method: "POST"},
            onSuccess: () => {
                notify({type: "success", text: "Запись подтверждена"});
                this.refreshCurrentContent();
            },
            onError: (message) => notify({type: "error", text: message || "Ошибка подтверждения записи"})
        });
    }

    handleContentWrapperClick(event) {
        const attachmentAddButton = event.target.closest(".attachment-add-button");
        if (attachmentAddButton && this.attachmentFileInput) {
            this.attachmentFileInput.dataset.recordId = attachmentAddButton.dataset.recordId;
            this.attachmentFileInput.click();
            return;
        }

        const attachmentDeleteButton = event.target.closest(".attachment-delete-button");
        if (attachmentDeleteButton) {
            this.deleteAttachment(
                attachmentDeleteButton.dataset.attachmentId,
                attachmentDeleteButton.dataset.filename
            );
            return;
        }

        const approveButton = event.target.closest(".competition-approve-button");
        if (approveButton) {
            this.reviewCompetition(approveButton.dataset.recordId, "approve");
            return;
        }

        const rejectButton = event.target.closest(".competition-reject-button");
        if (rejectButton) {
            this.reviewCompetition(rejectButton.dataset.recordId, "reject");
            return;
        }

        // CTA «Добавить участие» из пустого состояния атлета живёт внутри
        // content-wrapper и заменяется вместе с ним — поэтому ловится
        // делегированием, а не прямой привязкой (как тулбар «+ Пустая строка»).
        const emptyAddRowButton = event.target.closest(".empty-add-row-button");
        if (emptyAddRowButton) {
            this.startNewRowEdit();
            return;
        }

        const newRowSaveButton = event.target.closest(".new-row-save-button");
        if (newRowSaveButton) {
            this.saveNewRowEdit();
            return;
        }

        const newRowCancelButton = event.target.closest(".new-row-cancel-button");
        if (newRowCancelButton) {
            this.cancelNewRowEdit();
            return;
        }

        const saveButton = event.target.closest(".inline-save-button");
        if (saveButton) {
            this.saveInlineEdit();
            return;
        }

        const cancelButton = event.target.closest(".inline-cancel-button");
        if (cancelButton) {
            this.cancelInlineEdit();
            return;
        }

        const editButton = event.target.closest(".competition-edit-button");
        if (editButton) {
            this.startInlineEdit(editButton);
            return;
        }

        const deleteButton = event.target.closest(".competition-delete-button");
        if (deleteButton) {
            this.deleteCompetition(deleteButton.dataset.recordId, deleteButton.dataset.studentName);
        }
    }

    // Двойной клик по строке — вход в правку (замечание №9): кнопка
    // «Редактировать» закреплена в колонке действий, жест остаётся
    // ускорителем. Во время активной правки игнорируется — двойной клик
    // в полях ввода выделяет слово и не должен перезапускать правку.
    handleContentWrapperDblClick(event) {
        if (this.hasActiveRowEdit()) {
            return;
        }
        const row = event.target.closest("tr");
        if (!row || row.classList.contains("row-editing") || row.classList.contains("row-new")) {
            return;
        }
        const editButton = row.querySelector(".competition-edit-button");
        if (editButton) {
            this.startInlineEdit(editButton);
        }
    }

    importFile(formData) {
        this.makeRequest({
            url: "/",
            options: {
                method: "POST",
                body: formData,
            },
            onSuccess: (body) => {
                // Сводка импорта (main.py): «Импортировано N [. Пропущено
                // дублей: M] [. На подтверждение: K…]». Админу со спорными
                // строками — кнопка перехода в очередь подтверждения.
                const summary = body || "Файл успешно импортирован";
                const options = {type: "success", text: summary};
                if (document.body.dataset.role === "admin" && summary.includes("На подтверждение")) {
                    options.actionLabel = "Разобрать очередь";
                    options.actionUrl = "/admin/import-queue";
                }
                notify(options);
                this.cleanFileInput();
                this.refreshCurrentContent();
            },
            onError: (message) => notify({type: "error", text: message || "Ошибка импорта"})
        });
    }

    deleteCompetition(recordId, studentName) {
        const message = studentName
            ? `Удалить запись «${studentName}»?`
            : "Удалить запись?";
        const result = confirm(message);
        if (!result) {
            return;
        }
        this.makeRequest({
            url: `/competition/${recordId}/delete`,
            options: {
                method: "POST",
            },
            onSuccess: () => {
                notify({type: "success", text: "Запись удалена"});
                this.refreshCurrentContent();
            },
            onError: (message2) => notify({type: "error", text: message2 || "Ошибка удаления записи"})
        });
    }

    // Сброс одного условия отчёта (Reports v2) — крестик чипа: обычная
    // ссылка на /reports без параметров чипа, JS не участвует.

    getCsrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : "";
    }

    makeRequest({ url, options = {}, onSuccess = () => {}, onError = () => {} }) {
        const csrfToken = this.getCsrfToken();
        if (options.body instanceof FormData) {
            if (!options.body.has("csrf_token")) {
                options.body.append("csrf_token", csrfToken);
            }
        } else if (options.body instanceof URLSearchParams) {
            if (!options.body.has("csrf_token")) {
                options.body.append("csrf_token", csrfToken);
            }
        } else if (!options.body && String(options.method || "").toUpperCase() === "POST") {
            options.body = new URLSearchParams({csrf_token: csrfToken});
        }
        this.setLoading(true);
        fetch(url, options)
            .then((response) => {
                if (response.redirected && response.url.includes("/login")) {
                    window.location.href = response.url;
                    throw new Error("Redirected to login");
                }
                if (response.status === 401) {
                    window.location.href = "/login";
                    throw new Error("Unauthorized");
                }
                if (!response.ok) {
                    return response.text().then((message) => {
                        throw new Error(message);
                    });
                }
                return response.text();
            })
            .then((body) => {
                this.setLoading(false);
                const teamNotice = new DOMParser().parseFromString(body, "text/html").querySelector("[data-team-removal-notice]");
                if (teamNotice) notify({type: "warning", text: teamNotice.textContent.trim()});
                onSuccess(body);
            })
            .catch((error) => {
                this.setLoading(false);
                if (error.message === "Redirected to login" || error.message === "Unauthorized") {
                    return;
                }
                if (error.message && error.message.includes("CSRF")) {
                    notify({
                        type: "error",
                        text: "Сессия обновлена в другой вкладке. Обновите страницу и повторите действие",
                        actionLabel: "Обновить страницу",
                        actionHandler: () => window.location.reload(),
                    });
                    return;
                }
                onError(error.message);
            });
    }

    refreshCurrentContent() {
        // Reports v2: отчёт больше не обновляется fetch-ом — применённое
        // состояние это URL страницы, правок записей на /reports нет.
        this.refreshIndexContent();
    }

    refreshIndexContent() {
        // Реестр живёт в URL (фильтры, page/per_page — прототип 02): после
        // правок/создания/удаления перечитываем текущий адрес, а не «/»,
        // чтобы применённые условия и страница не терялись.
        this.setLoading(true);
        const currentUrl = window.location.pathname + window.location.search;
        fetch(currentUrl, {method: "GET"})
            .then((response) => response.text())
            .then((html) => {
                const doc = new DOMParser().parseFromString(html, "text/html");
                const nextContent = doc.querySelector(".content-wrapper");
                if (nextContent && this.contentWrapper) {
                    this.contentWrapper.replaceWith(nextContent);
                    this.initializeDomReferences();
                    this.bindContentWrapperEvents();
                    this.initTableFeatures();
                }
                this.setLoading(false);
            })
            .catch(() => {
                this.setLoading(false);
                window.location.reload();
            });
    }

    initTableFeatures() {
        // Футер таблицы («Показывать по») заменяется вместе с content-wrapper —
        // переинициализация нужна после каждой перерисовки списка; карточка
        // фильтров у себя внутри защищена повторной привязке флагом.
        this.initIndexFilterCard();
        this.tableCard = document.querySelector(".table-card");
        this.tableElement = document.querySelector(".interactive-table");
        this.renderStudentNamesList();
        this.inlineEditRow = null;
        this.inlineEditRecordId = null;
        this.inlineEditBackup = null;
        this.newEditRow = null;
        // При замене контента правка сбрасывается вместе со строкой — пикеры
        // и комбобоксы надо разрушить явно: их слушатели на document/body
        // переживают заменённый DOM.
        this.destroyRowDatePickers(this.inlineDatePickers);
        this.inlineDatePickers = null;
        this.destroyRowDatePickers(this.newRowDatePickers);
        this.newRowDatePickers = null;
        this.destroyRowComboBoxes();
        this.destroyFioResolvers();
        if (!this.tableElement) {
            if (this.tableColumnsManager) {
                this.tableColumnsManager.innerHTML = "";
            }
            return;
        }
        this.applyTableState();
        this.bindTableHeaderEvents();
        this.renderColumnManager();
    }

    applyTableState() {
        if (!this.tableElement) {
            return;
        }
        const state = this.readTableState();
        state.order.forEach((columnKey, targetIndex) => this.moveColumn(columnKey, targetIndex));
        this.setHiddenColumns(state.hidden);
        this.renderColumnManager();
    }

    getHeaderByKey(columnKey) {
        return this.tableElement.querySelector(`thead th[data-column-key="${columnKey}"]`);
    }

    moveColumn(columnKey, targetIndex) {
        const header = this.getHeaderByKey(columnKey);
        if (!header) {
            return;
        }
        const headers = Array.from(this.tableElement.querySelectorAll("thead th"));
        const currentIndex = headers.indexOf(header);
        if (currentIndex === -1 || currentIndex === targetIndex) {
            return;
        }

        if (this.tableElement.dataset.reportTable === "true") {
            // Rowspans make physical cell offsets differ between rows.
            const order = headers.map((item) => item.dataset.columnKey);
            order.splice(currentIndex, 1);
            order.splice(targetIndex, 0, columnKey);
            this.reorderReportCells(order);
            return;
        }
        const rows = Array.from(this.tableElement.querySelectorAll("tr"));
        rows.forEach((row) => {
            const cells = Array.from(row.children);
            const cell = cells[currentIndex];
            if (!cell) {
                return;
            }
            if (targetIndex >= cells.length - 1) {
                row.appendChild(cell);
            } else {
                row.insertBefore(cell, cells[targetIndex]);
            }
        });
    }

    reorderReportCells(order = Array.from(this.tableElement.querySelectorAll("thead th")).map((item) => item.dataset.columnKey)) {
        this.tableElement.querySelectorAll("tr").forEach((row) => {
            const cells = new Map(Array.from(row.children).map((cell) => [cell.dataset.columnKey, cell]));
            order.forEach((key) => {
                const cell = cells.get(key);
                if (cell) row.appendChild(cell);
            });
        });
    }

    setHiddenColumns(hiddenKeys) {
        if (!this.tableElement) {
            return;
        }
        const hiddenSet = new Set(hiddenKeys);
        this.tableElement.querySelectorAll("[data-column-key]").forEach((cell) => {
            cell.classList.toggle("table-column-hidden", hiddenSet.has(cell.dataset.columnKey));
        });
    }

    bindTableHeaderEvents() {
        // Реестр (data-server-sort) сортируется сервером: клик по заголовку —
        // GET-навигация с sort/order в URL (порядок применяется ко всей
        // отфильтрованной выборке). Таблица отчёта (report_result.html)
        // сохраняет клиентскую сортировку sortByHeader, как раньше.
        const serverSort = this.tableElement?.dataset.serverSort === "true";
        const headers = Array.from(this.tableElement.querySelectorAll("thead th"));
        headers.forEach((header) => {
            if (header.dataset.bound === "true") {
                return;
            }
            header.dataset.bound = "true";
            header.draggable = header.dataset.columnKey !== "actions";
            header.classList.add("table-header-cell");
            if (header.dataset.sortType !== "none") {
                if (serverSort) {
                    if (header.dataset.sortable === "true") {
                        header.addEventListener("click", () => this.handleServerSortClick(header));
                    }
                } else {
                    header.addEventListener("click", () => this.sortByHeader(header));
                    if (this.tableElement.dataset.reportTable === "true") {
                        header.tabIndex = 0;
                        header.setAttribute("aria-sort", "none");
                        header.addEventListener("keydown", (event) => {
                            if (event.key === "Enter" || event.key === " ") {
                                event.preventDefault();
                                this.sortByHeader(header);
                            }
                        });
                    }
                }
            }
            header.addEventListener("dragstart", (event) => this.handleHeaderDragStart(event, header));
            header.addEventListener("dragover", (event) => this.handleHeaderDragOver(event, header));
            header.addEventListener("drop", (event) => this.handleHeaderDrop(event, header));
        });
    }

    // Серверная сортировка реестра: сохраняем текущие query-параметры
    // (фильтры, per_page — паттерн per-page-select), page сбрасываем — новая
    // сортировка возвращает на первую страницу. Активная колонка меняет
    // направление (asc → desc), новая — всегда asc. Индикатор и aria-sort
    // рендерит сервер по sort/order из URL.
    handleServerSortClick(header) {
        if (this.hasActiveRowEdit()) {
            return;
        }
        const nextOrder = header.dataset.sortDirection === "asc" ? "desc" : "asc";
        const params = new URLSearchParams(window.location.search);
        params.delete("page");
        params.set("sort", header.dataset.columnKey);
        params.set("order", nextOrder);
        window.location.href = `/?${params.toString()}`;
    }

    handleHeaderDragStart(event, header) {
        this.draggedColumnKey = header.dataset.columnKey;
        event.dataTransfer.effectAllowed = "move";
    }

    handleHeaderDragOver(event) {
        event.preventDefault();
    }

    handleHeaderDrop(event, targetHeader) {
        event.preventDefault();
        if (this.hasActiveRowEdit()) {
            return;
        }
        // Колонка «Действия» закреплена (sticky right) и должна оставаться
        // последней — иначе закреплённая ячейка перекроет соседнюю колонку.
        if (!this.draggedColumnKey
            || this.draggedColumnKey === targetHeader.dataset.columnKey
            || targetHeader.dataset.columnKey === "actions") {
            return;
        }
        const order = Array.from(this.tableElement.querySelectorAll("thead th")).map((header) => header.dataset.columnKey);
        const sourceIndex = order.indexOf(this.draggedColumnKey);
        const targetIndex = order.indexOf(targetHeader.dataset.columnKey);
        const [columnKey] = order.splice(sourceIndex, 1);
        order.splice(targetIndex, 0, columnKey);
        const state = this.readTableState();
        state.order = order;
        this.saveTableState(state);
        this.applyTableState();
    }

    sortByHeader(header) {
        if (this.hasActiveRowEdit() || header.dataset.sortType === "none") {
            return;
        }
        const isReport = this.tableElement.dataset.reportTable === "true";
        const columnKey = header.dataset.columnKey;
        const sortType = header.dataset.sortType || "text";
        const currentDirection = header.dataset.sortDirection === "asc" ? "desc" : "asc";
        this.tableElement.querySelectorAll("thead th").forEach((item) => {
            item.dataset.sortDirection = "";
            item.classList.remove("sorted-asc", "sorted-desc");
            if (isReport) item.removeAttribute("aria-sort");
        });
        if (isReport) header.setAttribute("aria-sort", currentDirection === "asc" ? "ascending" : "descending");
        header.dataset.sortDirection = currentDirection;
        header.classList.add(currentDirection === "asc" ? "sorted-asc" : "sorted-desc");

        const body = this.tableElement.querySelector("tbody");
        const rows = Array.from(body.querySelectorAll("tr"));
        if (isReport) {
            this.sortReportRows(body, rows, columnKey, sortType, currentDirection);
            return;
        }
        rows.sort((first, second) => {
            // data-sort-value (если задан) — машинное значение ячейки:
            // «Дата» отображается компактным диапазоном («25-27.06.2026»),
            // data-sort-value у неё — ISO-дата начала (parseDateValue умеет
            // оба формата: ISO и дд.мм.гггг кастомных date-полей).
            const firstCell = first.querySelector(`[data-column-key="${columnKey}"]`);
            const secondCell = second.querySelector(`[data-column-key="${columnKey}"]`);
            const firstValue = firstCell?.dataset.sortValue || firstCell?.textContent.trim() || "";
            const secondValue = secondCell?.dataset.sortValue || secondCell?.textContent.trim() || "";
            return this.compareValues(firstValue, secondValue, sortType, currentDirection);
        });
        rows.forEach((row) => body.appendChild(row));
    }

    sortReportRows(body, rows, columnKey, sortType, direction) {
        // Group IDs come from the server's confirmed identity + exact name.
        // Never infer identity from link text, numeric totals or current profile.
        const groups = new Map();
        rows.forEach((row) => {
            const key = row.dataset.reportGroup;
            if (!groups.has(key)) groups.set(key, {rows: [], nameCell: null});
            const group = groups.get(key);
            group.rows.push(row);
            group.nameCell ||= row.querySelector('[data-column-key="student_name"]');
        });
        const value = (row, group) => {
            const cell = columnKey === "student_name" ? group.nameCell : row.querySelector(`[data-column-key="${columnKey}"]`);
            return cell?.dataset.sortValue ?? cell?.textContent.trim() ?? "";
        };
        const compare = (first, firstGroup, second, secondGroup) =>
            this.compareValues(value(first, firstGroup), value(second, secondGroup), sortType, direction);
        const blocks = Array.from(groups.values());
        // Native stable sorting preserves ties, including zero-valued metrics.
        blocks.forEach((group) => group.rows.sort((a, b) => compare(a, group, b, group)));
        blocks.sort((a, b) => compare(a.rows[0], a, b.rows[0], b));
        let index = 0;
        blocks.forEach((group) => {
            if (group.nameCell) {
                group.nameCell.rowSpan = group.rows.length;
                group.rows[0].appendChild(group.nameCell);
            }
            group.rows.forEach((row, offset) => {
                row.classList.toggle("report-group-start", offset === 0);
                row.querySelector('[data-column-key="index"]').textContent = String(++index);
                body.appendChild(row);
            });
        });
        this.reorderReportCells();
    }

    compareValues(firstValue, secondValue, sortType, direction) {
        let result = 0;
        if (sortType === "number") {
            result = Number(firstValue || 0) - Number(secondValue || 0);
        } else if (sortType === "date") {
            result = this.parseDateValue(firstValue) - this.parseDateValue(secondValue);
        } else {
            result = firstValue.localeCompare(secondValue, "ru", { sensitivity: "base" });
        }
        return direction === "asc" ? result : -result;
    }

    parseDateValue(value) {
        const text = String(value || "").trim();
        // Два формата дат таблиц: ISO «2026-06-25» (data-sort-value колонки
        // «Дата» реестра) и дд.мм.гггг (кастомные date-поля). Невалидное
        // значение → NaN: ECMA-262 трактует NaN-компаратор как +0, такие
        // ячейки сравниваются как равные и не ломают сортировку.
        const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
        if (iso) {
            return new Date(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3])).getTime();
        }
        const [day, month, year] = text.split(".");
        return new Date(Number(year || 0), Number(month || 1) - 1, Number(day || 1)).getTime();
    }

    renderColumnManager() {
        if (!this.tableColumnsManager || !this.tableElement) {
            return;
        }
        const state = this.readTableState();
        const hidden = new Set(state.hidden);
        const headers = Array.from(this.tableElement.querySelectorAll("thead th"))
            .filter((header) => header.dataset.columnKey !== "actions");

        this.tableColumnsManager.replaceChildren(...headers.map((header) => {
            const wrapper = document.createElement("div");
            wrapper.className = "col-md-4";
            const label = document.createElement("label");
            label.className = "form-check table-column-option";
            const input = document.createElement("input");
            input.className = "form-check-input table-column-toggle";
            input.type = "checkbox";
            input.dataset.columnKey = header.dataset.columnKey;
            input.checked = !hidden.has(header.dataset.columnKey);
            const span = document.createElement("span");
            span.className = "form-check-label";
            span.textContent = header.textContent.trim();
            label.append(input, span);
            wrapper.append(label);
            return wrapper;
        }));

        this.tableColumnsManager.querySelectorAll(".table-column-toggle").forEach((input) => {
            input.addEventListener("change", () => {
                const nextHidden = Array.from(this.tableColumnsManager.querySelectorAll(".table-column-toggle"))
                    .filter((checkbox) => !checkbox.checked)
                    .map((checkbox) => checkbox.dataset.columnKey);
                const nextState = this.readTableState();
                nextState.hidden = nextHidden;
                this.saveTableState(nextState);
                this.applyTableState();
            });
        });
    }

    getVisibleExportColumns() {
        if (!this.tableElement) {
            return [];
        }
        return Array.from(this.tableElement.querySelectorAll("thead th"))
            .filter((header) => !header.classList.contains("table-column-hidden"))
            .filter((header) => header.dataset.exportable !== "false")
            .map((header) => ({
                key: header.dataset.columnKey,
                label: header.textContent.trim(),
            }));
    }

    exportCurrentTable() {
        if (!this.tableElement) {
            notify({type: "warning", text: "Нет данных для выгрузки"});
            return;
        }
        this.cancelInlineEdit();
        this.cancelNewRowEdit();

        const columns = this.getVisibleExportColumns();
        const rows = Array.from(this.tableElement.querySelectorAll("tbody tr")).map((row) =>
            columns.map((column) => {
                const cell = row.querySelector(`[data-column-key="${column.key}"]`);
                return cell ? cell.textContent.trim() : "";
            })
        );

        const tableHtml = `
            <table>
                <thead>
                    <tr>${columns.map((column) => `<th>${this.escapeHtml(column.label)}</th>`).join("")}</tr>
                </thead>
                <tbody>
                    ${rows.map((row) => `<tr>${row.map((value) => `<td>${this.escapeHtml(value)}</td>`).join("")}</tr>`).join("")}
                </tbody>
            </table>
        `;
        const documentHtml = `
            <html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:x="urn:schemas-microsoft-com:office:excel">
                <head><meta charset="utf-8"></head>
                <body>${tableHtml}</body>
            </html>
        `;
        const blob = new Blob([documentHtml], { type: "application/vnd.ms-excel;charset=utf-8;" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        const now = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 16);
        link.href = url;
        link.download = `competitions-${this.getCurrentView()}-${now}.xls`;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        URL.revokeObjectURL(url);
    }

    escapeHtml(value) {
        return value
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;");
    }

    setLoading(isActive) {
        if (!this.loader) {
            return;
        }
        this.loader.classList.toggle("loader-active", isActive);
    }
}

window.addEventListener("DOMContentLoaded", () => {
    new Main();
    const personPage = document.querySelector("[data-person-page]");
    if (personPage) new StudentProfilePage(personPage);
    initCalendarPeriodPickers();
    initFlashAutoHide();
    initEventLinksRepeaters();
    // Страница «Студенты» (админ): подсказки в поле поиска — только карточки
    // (onlyStudents), выбор просто подставляет ФИО в поле (onSelect пуст) —
    // свободный ввод остаётся валидным, скрытых полей связи нет: поиск по
    // подстроке, а не выбор конкретной карточки.
    const peopleSearchInput = document.querySelector("[data-people-search]");
    if (peopleSearchInput) {
        new FioResolver(peopleSearchInput, () => {}, {onlyStudents: true});
    }
    // Страница участников соревнования (волна B, прототип 16): резолвер ФИО
    // в строке добавления + инлайн-правка строки (место «дописать позже»).
    if (document.querySelector("[data-participants-page]")) {
        new ParticipantsPage();
    }
});

// Период календаря (calendar.html / calendar_event.html): единый всплывающий
// календарь RangePicker (отзыв владельца: «выбор диапазона 2-мя нажатиями на
// 1 календарь, в стиле главной таблицы»). Видимое readonly-поле показывает
// «DD.MM.YYYY» или «DD.MM.YYYY – DD.MM.YYYY», на submit в скрытое поле date
// уходит то же значение через дефис («DD.MM.YYYY-DD.MM.YYYY») — контракт
// POST /calendar/new и /calendar/<id>/edit не меняется, серверная валидация
// остаётся источником истины.
//
// Выбор: 1-й клик = начало, 2-й клик = конец (hover подсвечивает отрезок).
// Повторный клик по той же дате = однодневный диапазон и закрытие панели,
// поэтому одинарный режим отдельного переключателя не требует. Закрытие
// по Esc/клику вне панели при выбранном только начале фиксирует его
// как однодневную дату (прощение, а не отмена: пустое «по» = однодневное).

const RP_MONTH_NAMES = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
];
const RP_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"];

function rpIsoToParts(iso) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || "").trim());
    if (!match) {
        return null;
    }
    return { y: Number(match[1]), m: Number(match[2]) - 1, d: Number(match[3]) };
}

function rpToTime(parts) {
    // Полдень локального дня: сравнения и сдвиги без сюрпризов DST/полуночи.
    return new Date(parts.y, parts.m, parts.d, 12).getTime();
}

function initCalendarPeriodPickers() {
    document.querySelectorAll("input[data-range-picker]").forEach((input) => {
        if (input.dataset.rpBound === "true") {
            return;
        }
        input.dataset.rpBound = "true";
        new RangePicker(input);
    });
}

class RangePicker {
    constructor(input) {
        this.input = input;
        const form = input.closest("form");
        this.hidden = form ? form.querySelector('input[name="date"][data-period-field]') : null;
        this.start = null;
        this.end = null;
        this.panel = null;
        this.onDocClick = null;
        this.onDocKey = null;
        const from = rpIsoToParts(input.dataset.rangeFrom);
        const to = rpIsoToParts(input.dataset.rangeTo);
        if (from) {
            this.start = from;
            this.end = to && rpToTime(to) > rpToTime(from) ? to : null;
        }
        this.input.addEventListener("click", () => this.open());
        // readonly-поле не участвует в constraint-валидации (required на нём
        // браузер игнорирует), поэтому пустой сабмит перехватываем сами:
        // вместо отправки открываем пикер.
        if (form) {
            form.addEventListener("submit", (event) => {
                if (!this.start) {
                    event.preventDefault();
                    this.open();
                    this.input.focus();
                }
            });
        }
        this.input.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
                event.preventDefault();
                this.toggle();
            } else if (event.key === "Escape") {
                this.close();
            }
        });
        this.syncValue();
    }

    toggle() {
        if (this.panel) {
            this.close();
        } else {
            this.open();
        }
    }

    open() {
        if (this.panel) {
            return;
        }
        const anchor = this.start || rpIsoToParts(
            new Date().toLocaleDateString("sv-SE") // локальная today как YYYY-MM-DD
        );
        this.viewYear = anchor.y;
        this.viewMonth = anchor.m;
        this.panel = document.createElement("div");
        this.panel.className = "range-picker-panel";
        this.panel.setAttribute("role", "dialog");
        this.panel.setAttribute("aria-label", "Выбор периода");
        document.body.appendChild(this.panel);
        this.render();
        this.position();
        this.onDocClick = (event) => {
            if (!this.panel.contains(event.target) && event.target !== this.input) {
                this.close();
            }
        };
        this.onDocKey = (event) => {
            if (event.key === "Escape") {
                this.close();
            }
        };
        document.addEventListener("mousedown", this.onDocClick, true);
        document.addEventListener("keydown", this.onDocKey, true);
    }

    close() {
        if (!this.panel) {
            return;
        }
        document.removeEventListener("mousedown", this.onDocClick, true);
        document.removeEventListener("keydown", this.onDocKey, true);
        this.onDocClick = null;
        this.onDocKey = null;
        this.panel.remove();
        this.panel = null;
        this.syncValue();
    }

    position() {
        const rect = this.input.getBoundingClientRect();
        const width = this.panel.offsetWidth;
        const height = this.panel.offsetHeight;
        let left = rect.left;
        if (left + width > window.innerWidth - 8) {
            left = Math.max(8, window.innerWidth - width - 8);
        }
        let top = rect.bottom + 4;
        if (top + height > window.innerHeight - 8 && rect.top - height - 4 >= 8) {
            top = rect.top - height - 4;
        }
        this.panel.style.left = `${Math.round(left)}px`;
        this.panel.style.top = `${Math.round(top)}px`;
    }

    render() {
        const first = new Date(this.viewYear, this.viewMonth, 1, 12);
        // Сдвиг к понедельнику (неделя с пн, как в остальной таблице).
        let lead = (first.getDay() + 6) % 7;
        const cells = [];
        const cursor = new Date(first);
        cursor.setDate(cursor.getDate() - lead);
        const todayTime = rpToTime(rpIsoToParts(new Date().toLocaleDateString("sv-SE")));
        const startTime = this.start ? rpToTime(this.start) : null;
        const endTime = this.end ? rpToTime(this.end) : null;
        for (let i = 0; i < 42; i += 1) {
            const parts = { y: cursor.getFullYear(), m: cursor.getMonth(), d: cursor.getDate() };
            const time = rpToTime(parts);
            const outside = parts.m !== this.viewMonth;
            let state = "";
            if (outside) {
                state = " is-outside";
            }
            if (time === todayTime) {
                state += " is-today";
            }
            const inRange = startTime !== null && endTime !== null
                && time > startTime && time < endTime;
            if (inRange) {
                state += " is-in-range";
            }
            if ((startTime !== null && time === startTime)
                || (endTime !== null && time === endTime)) {
                state += " is-edge";
                if (startTime !== null && time === startTime
                    && endTime !== null && time === endTime) {
                    state += " is-single";
                }
            }
            cells.push(`<button type="button" class="rp-day${state}" data-day="${parts.y}-`
                + `${String(parts.m + 1).padStart(2, "0")}-${String(parts.d).padStart(2, "0")}"`
                + `${outside ? " tabindex=\"-1\"" : ""}>${parts.d}</button>`);
            cursor.setDate(cursor.getDate() + 1);
        }
        const dows = RP_WEEKDAYS.map((d) => `<span class="rp-dow">${d}</span>`).join("");
        this.panel.innerHTML = `
            <div class="rp-head">
                <button type="button" class="rp-nav" data-nav="-1" aria-label="Предыдущий месяц">&#8249;</button>
                <span class="rp-title">${RP_MONTH_NAMES[this.viewMonth]} ${this.viewYear}</span>
                <button type="button" class="rp-nav" data-nav="1" aria-label="Следующий месяц">&#8250;</button>
            </div>
            <div class="rp-grid">${dows}${cells.join("")}</div>
            <div class="rp-hint">Два клика: начало и конец. Повторный клик по той же дате — однодневное.</div>`;
        this.panel.querySelectorAll("[data-nav]").forEach((btn) => {
            btn.addEventListener("click", () => {
                this.viewMonth += Number(btn.dataset.nav);
                if (this.viewMonth < 0) {
                    this.viewMonth = 11;
                    this.viewYear -= 1;
                } else if (this.viewMonth > 11) {
                    this.viewMonth = 0;
                    this.viewYear += 1;
                }
                this.render();
                this.position();
            });
        });
        this.panel.querySelectorAll(".rp-day").forEach((btn) => {
            btn.addEventListener("mouseenter", () => this.previewHover(btn));
            btn.addEventListener("click", () => this.pickDay(btn.dataset.day));
        });
    }

    previewHover(btn) {
        // Hover-подсветка отрезка между первым кликом и текущей ячейкой.
        const day = rpIsoToParts(btn.dataset.day);
        if (!day || !this.start || this.end) {
            return;
        }
        const startTime = rpToTime(this.start);
        const dayTime = rpToTime(day);
        this.panel.querySelectorAll(".rp-day").forEach((cell) => {
            const parts = rpIsoToParts(cell.dataset.day);
            const time = rpToTime(parts);
            const between = dayTime > startTime
                ? time > startTime && time < dayTime
                : time < startTime && time > dayTime;
            cell.classList.toggle("is-in-range", between);
            cell.classList.toggle("is-edge-preview", time === dayTime);
        });
    }

    pickDay(iso) {
        const day = rpIsoToParts(iso);
        if (!day) {
            return;
        }
        if (!this.start || (this.start && this.end)) {
            // Новый выбор (или перезапуск, если кликнули раньше начала).
            this.start = day;
            this.end = null;
            this.render();
            return;
        }
        if (rpToTime(day) < rpToTime(this.start)) {
            // Конец раньше начала — начинаем выбор заново с этой даты.
            this.start = day;
            this.end = null;
            this.render();
            return;
        }
        this.end = rpToTime(day) === rpToTime(this.start) ? null : day;
        this.syncValue();
        this.close();
    }

    syncValue() {
        const fmt = (p) => `${String(p.d).padStart(2, "0")}.${String(p.m + 1).padStart(2, "0")}.${p.y}`;
        let display = "";
        let composed = "";
        if (this.start) {
            display = fmt(this.start);
            composed = display;
            if (this.end) {
                display += ` – ${fmt(this.end)}`;
                composed += `-${fmt(this.end)}`;
            }
        }
        this.input.value = display;
        if (this.hidden) {
            this.hidden.value = composed;
        }
    }
}

// Привязка пикеров периода к формам календаря (планирование, инлайн-правки,
// правка на /calendar/<id>): поле ввода readonly + скрытое поле date.

// Страница участников соревнования (волна B, docs/feedback-live.md №23).
// Переиспользует FioResolver (выбор варианта подставляет пол/институт/
// группу/курс — поля остаются редактируемыми) и те же эндпоинты записей
// реестра, что и инлайн-правка главной: сохранение — POST /competition/<id>,
// удаление — POST /competition/<id>/delete. Название/даты/уровень/спорт
// в правке не редактируются — берутся из data-* атрибутов таблицы (пресет
// события), историчность записи не нарушается.
class ParticipantsPage {
    constructor() {
        this.table = document.querySelector("[data-participants-page]");
        this.editingRow = null;
        this.editingBackup = null;
        this.editingSubRow = null;
        this.keydownHandler = null;
        this.activeFioResolver = null;
        // Защита от двойного сабмита правки участника (см. saveEdit).
        this.participantSaving = false;
        // P7: participation-кастомы (label + key) — инпуты правки участия.
        this.participationCustomFields = this.parseParticipationCustomFields();
        this.attachAddFormResolver();
        this.initEventView();
        this.table.addEventListener("click", (event) => this.handleClick(event));
        // Защита несохранённого ввода (UX P0): инлайн-правка участия —
        // браузерное подтверждение при закрытии/перезагрузке вкладки.
        window.addEventListener("beforeunload", (event) => {
            if (this.editingRow || this.addDirty) {
                event.preventDefault();
                event.returnValue = "";
            }
        });
    }


    // Progressive enhancement: without JS all sections and normal forms remain reachable.
    initEventView() {
        this.root = this.table.closest("[data-event-page]");
        if (!this.root) return;
        this.root.classList.add("event-enhanced");
        this.addDirty = false;
        this.viewKey = `competitions:event-view:${this.root.dataset.eventPage}`;
        this.search = this.root.querySelector("#event-participant-search");
        this.search.value = (new URLSearchParams(location.search).get("q") || "").slice(0, 2000);
        this.tabs = [...this.root.querySelectorAll("[data-event-tab]")];
        const nav = this.root.querySelector(".event-tabs");
        nav.setAttribute("role", "tablist");
        this.tabs.forEach((tab) => {
            const key = tab.dataset.eventTab;
            const panel = this.root.querySelector(`[data-event-panel="${key}"]`);
            panel.id ||= `event-panel-${key}`;
            panel.setAttribute("role", "tabpanel");
            panel.setAttribute("aria-labelledby", `event-tab-${key}`);
            tab.id = `event-tab-${key}`;
            tab.setAttribute("role", "tab");
            tab.setAttribute("aria-controls", panel.id);
            tab.addEventListener("click", (event) => { event.preventDefault(); this.showTab(key); });
            tab.addEventListener("keydown", (event) => {
                if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                event.preventDefault();
                const index = this.tabs.indexOf(tab);
                const next = event.key === "Home" ? 0 : event.key === "End" ? this.tabs.length - 1
                    : (index + (event.key === "ArrowRight" ? 1 : -1) + this.tabs.length) % this.tabs.length;
                if (this.showTab(this.tabs[next].dataset.eventTab) !== false) this.tabs[next].focus();
            });
        });
        const params = new URLSearchParams(location.search);
        const forced = this.root.dataset.initialTab;
        const hashTab = {"#documents": "documents", "#protocols": "documents", "#event-documents": "documents", "#team-results": "teams", "#event-teams": "teams", "#event-about": "about"}[location.hash];
        const initial = forced !== "participants" ? forced : (hashTab || params.get("tab") || "participants");
        this.showTab(initial, false);
        window.addEventListener("hashchange", () => {
            const key = {"#documents": "documents", "#protocols": "documents", "#event-documents": "documents", "#team-results": "teams", "#event-teams": "teams", "#event-about": "about"}[location.hash];
            if (key) this.showTab(key, false);
        });
        this.setAddOpen(this.root.dataset.addOpen === "true");
        this.root.querySelector("[data-event-toggle-add]")?.addEventListener("click", () => {
            if (this.editingRow) { notify({type: "error", text: "Сохраните или отмените текущую правку участия."}); return; }
            if (this.addDirty) { notify({type: "error", text: "Сохраните участника или нажмите «Отменить добавление»."}); return; }
            this.showTab("participants");
            this.setAddOpen(!this.root.classList.contains("event-add-open"));
            if (this.root.classList.contains("event-add-open")) this.table.querySelector(".row-new input:not([type=hidden])")?.focus();
        });
        const form = document.getElementById("participant-add-form");
        this.root.querySelector("[data-event-cancel-add]")?.addEventListener("click", () => {
            form?.reset();
            document.getElementById("participant-student-reset")?.click();
            this.addDirty = false;
            this.setAddOpen(false);
            this.root.querySelector("[data-event-toggle-add]")?.focus();
        });
        if (form) {
            this.table.querySelectorAll('[form="participant-add-form"]').forEach((input) => {
                input.addEventListener("input", () => { this.addDirty = true; });
            });
            form.addEventListener("submit", () => {
                this.addDirty = false;
                this.storeView();
                this.table.querySelectorAll('[type="submit"][form="participant-add-form"]').forEach(button => { button.disabled = true; });
            });
        }
        this.root.querySelector("[data-event-open-edit]")?.addEventListener("click", () => {
            if (this.showTab("about") === false) return;
            const details = this.root.querySelector("#event-edit-form");
            details.open = true;
            details.querySelector("input:not([type=hidden])")?.focus();
        });
        this.search.addEventListener("input", () => this.filterParticipants());
        this.root.querySelector("#event-result-filter").addEventListener("change", (event) => this.changeResultFilter(event.target.value));
        this.root.querySelectorAll("[data-event-result-link]").forEach(link => link.addEventListener("click", event => {
            event.preventDefault(); this.changeResultFilter(link.href);
        }));
        this.root.querySelectorAll("[data-event-clear-search]").forEach(control => control.addEventListener("click", event => {
            event.preventDefault();
            if (this.editingRow || this.addDirty) { notify({type: "error", text: "Сохраните или отмените текущий ввод перед сбросом фильтров."}); return; }
            this.search.value = "";
            try { sessionStorage.removeItem(this.viewKey); } catch (error) { /* Optional storage. */ }
            if (this.root.dataset.resultFilter) this.changeResultFilter(`/calendar/${this.root.dataset.eventPage}`);
            else { this.filterParticipants(); this.search.focus(); }
        }));
        this.root.querySelectorAll("form").forEach(formElement => formElement.addEventListener("submit", event => { if (!event.defaultPrevented) this.storeView(); }));
        this.decorateParticipantCells();
        this.filterParticipants();
        try {
            const stored = JSON.parse(sessionStorage.getItem(this.viewKey) || "null");
            sessionStorage.removeItem(this.viewKey);
            if (stored && Date.now() - stored.at >= 0 && Date.now() - stored.at < 60000) {
                // Native POST redirects do not know client search. Restore its one-shot UI
                // context only when the destination did not explicitly choose a query.
                if (!params.has("q") && typeof stored.query === "string") {
                    this.search.value = stored.query.slice(0, 2000);
                    this.filterParticipants();
                }
                // Server deep-link/edit states take precedence over remembered tab/scroll.
                if (forced === "participants" && !hashTab && !params.get("tab")) { this.showTab(stored.tab || "participants", false); this.updateViewURL(); }
                if (forced === "participants" && !hashTab) requestAnimationFrame(() => {
                    const button = this.table.querySelector(`.participant-edit-button[data-record-id="${Number(stored.recordId)}"]`);
                    (button || this.root.querySelector("#event-result-filter"))?.focus({preventScroll: true});
                    window.scrollTo(0, Number(stored.scrollY) || 0);
                    const wrap = this.table.closest(".table-responsive");
                    if (wrap) wrap.scrollLeft = Number(stored.scrollX) || 0;
                });
            }
        } catch (error) { /* Storage may be disabled; normal navigation remains available. */ }
        if (hashTab) requestAnimationFrame(() => document.getElementById(location.hash.slice(1))?.scrollIntoView({block: "start"}));
    }

    showTab(key, updateURL = true) {
        if (updateURL && (this.editingRow || this.addDirty)) {
            notify({type: "error", text: this.editingRow ? "Сохраните или отмените текущую правку участия." : "Сохраните участника или отмените добавление."});
            return false;
        }
        if (!this.tabs.some(tab => tab.dataset.eventTab === key)) key = "participants";
        this.activeTab = key;
        this.tabs.forEach(tab => {
            const active = tab.dataset.eventTab === key;
            tab.setAttribute("aria-selected", String(active));
            tab.tabIndex = active ? 0 : -1;
        });
        this.root.querySelectorAll("[data-event-panel]").forEach(panel => { panel.hidden = panel.dataset.eventPanel !== key; });
        if (updateURL) this.updateViewURL(true);
    }

    updateViewURL(clearHash = false) {
        const url = new URL(location.href);
        if (clearHash) url.hash = "";
        if (this.search.value) url.searchParams.set("q", this.search.value); else url.searchParams.delete("q");
        if (this.activeTab !== "participants") url.searchParams.set("tab", this.activeTab); else url.searchParams.delete("tab");
        history.replaceState(null, "", url);
    }

    storeView(recordId = null) {
        // One-shot UI context (60s): viewport, tab, typed search only. Never row/form drafts or account data.
        try { sessionStorage.setItem(this.viewKey, JSON.stringify({at: Date.now(), tab: this.activeTab,
            scrollY: window.scrollY, scrollX: this.table.closest(".table-responsive")?.scrollLeft || 0, recordId, query: this.search.value.slice(0, 2000)})); }
        catch (error) { /* Optional view restoration. */ }
    }

    changeResultFilter(target) {
        if (this.editingRow || this.addDirty) {
            const filter = this.root.querySelector("#event-result-filter");
            const appliedOption = [...filter.options].find(option => option.defaultSelected);
            if (appliedOption) filter.value = appliedOption.value;
            notify({type: "error", text: "Сохраните или отмените текущий ввод перед сменой фильтра места."});
            return;
        }
        const url = new URL(target, location.origin);
        const returnContext = new URL(location.href).searchParams.get("return");
        if (returnContext) url.searchParams.set("return", returnContext);
        if (this.search.value) url.searchParams.set("q", this.search.value);
        this.storeView();
        window.location.href = url;
    }

    setAddOpen(open) {
        this.root.classList.toggle("event-add-open", open);
        this.root.querySelector("[data-event-toggle-add]")?.setAttribute("aria-expanded", String(open));
    }

    decorateParticipantCells() {
        const labels = ["№", "ФИО", "Пол", "Институт", "Группа", "Курс", "Дисциплина", "Место", "Результат", "Действия"];
        this.table.querySelectorAll("tbody tr").forEach(row => {
            let index = 0;
            [...row.children].forEach(cell => {
                cell.dataset.eventCol = String(index);
                cell.dataset.label = labels[index] || "Дополнительные поля";
                index += cell.colSpan || 1;
            });
        });
    }

    filterParticipants() {
        const term = this.search.value.trim().toLocaleLowerCase("ru");
        let records = 0;
        const groups = new Set();
        this.table.querySelectorAll("[data-event-group]").forEach(row => {
            const found = `${row.dataset.searchName} ${row.dataset.searchGroup}`.toLocaleLowerCase("ru").includes(term);
            // An active edit must never disappear while its draft is being entered.
            row.hidden = !found && row !== this.editingRow;
            if (found && row.dataset.recordId) { records += 1; groups.add(row.dataset.eventGroup); }
        });
        const count = this.root.querySelector("#event-search-count");
        count.textContent = `В выборке: ${groups.size} участников · ${records} участий`;
        this.root.querySelector(".event-search-empty").hidden = !term || records > 0;
        this.updateViewURL();
    }

    parseParticipationCustomFields() {
        const raw = this.table.dataset.participationCustomFields;
        if (!raw) {
            return [];
        }
        try {
            const parsed = JSON.parse(raw);
            return Array.isArray(parsed) ? parsed : [];
        } catch (error) {
            return [];
        }
    }

    getCsrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : "";
    }

    request(url, {body, onSuccess = () => {}, onError = () => {}}) {
        if (body instanceof FormData && !body.has("csrf_token")) {
            body.append("csrf_token", this.getCsrfToken());
        }
        fetch(url, {method: "POST", body})
            .then((response) => {
                if (response.redirected && new URL(response.url).pathname === "/login") throw new Error("Сессия истекла. Войдите заново; введённые значения остаются в форме.");
                if (!response.ok) {
                    return response.text().then((message) => {
                        throw new Error(message);
                    });
                }
                return response.text();
            })
            .then((html) => {
                const teamNotice = new DOMParser().parseFromString(html, "text/html").querySelector("[data-team-removal-notice]");
                this.teamRemovalMessage = teamNotice ? teamNotice.textContent.trim() : "";
                onSuccess(html);
            })
            .catch((error) => onError(error.message));
    }

    // Резолвер ФИО в строке добавления (та же логика, что у инлайн-строки
    // главной): выбор варианта подставляет известные значения, поля
    // остаются редактируемыми. Свободный ввод — как на главной. Варианты
    // включают карточки Students: явный выбор карточки заполняет пустые
    // пол/институт/группу/курс и пишет student_ref_id в скрытое поле;
    // правка ФИО после выбора молча сбрасывает связь (имя записи — снимок
    // ввода, связь имеет смысл только для выбранного написания).
    attachAddFormResolver() {
        const fioInput = document.getElementById("participant-fio");
        // P7: readonly/отсутствие поля = prefill-режим «+ участие» (ФИО —
        // hidden-снимок) или страница без формы — резолвер не нужен.
        if (!fioInput || fioInput.readOnly || fioInput.disabled
            || document.body.dataset.role === "athlete") {
            return;
        }
        const refInput = document.getElementById("participant-student-ref");
        const badge = document.getElementById("participant-student-badge");
        const resetLink = document.getElementById("participant-student-reset");
        let selectedStudent = null;
        const hintedInputs = [];

        // P0.1: re-fill после ошибки формы — сервер вернул ВАЛИДНУЮ карточку
        // в скрытое поле (связь не должна молча теряться при повторном
        // сабмите). Восстанавливаем состояние «карточка выбрана»: бейдж виден,
        // правка ФИО и «сбросить связь» работают как после выбора в
        // резолвере. Мусорное значение поля игнорируется (не-число/пустое
        // ФИО — карточка не считается выбранной).
        const restoredRef = refInput ? refInput.value.trim() : "";
        if (restoredRef && /^\d+$/.test(restoredRef) && fioInput.value.trim()) {
            selectedStudent = {name: fioInput.value, student_id: Number(restoredRef)};
            if (badge) {
                badge.classList.remove("d-none");
            }
        }

        const hideBadge = () => {
            if (badge) {
                badge.classList.add("d-none");
            }
        };
        const clearTitleHints = () => {
            hintedInputs.forEach((input) => input.removeAttribute("title"));
            hintedInputs.length = 0;
        };
        const setStudent = (student) => {
            selectedStudent = student;
            if (refInput) {
                refInput.value = student ? String(student.student_id) : "";
            }
            clearTitleHints();
            if (!student) {
                hideBadge();
            }
        };

        const fill = (athlete, athleteKey, inputId) => {
            const value = athlete[athleteKey];
            const input = value ? document.getElementById(inputId) : null;
            if (input && !input.value) {
                input.value = value;
                if (athlete.kind === "student") {
                    input.title = "Подставлено из карточки студента";
                    hintedInputs.push(input);
                }
            }
        };

        new FioResolver(fioInput, (athlete) => {
            // Сначала связь (сброс прежних подсказок-титулов), затем
            // автозаполнение — оно расставит титулы выбранной карточки.
            if (athlete.kind === "student") {
                setStudent(athlete);
                if (badge) {
                    badge.classList.remove("d-none");
                }
            } else {
                setStudent(null);
            }
            fill(athlete, "sex", "participant-sex");
            fill(athlete, "institute", "participant-institute");
            fill(athlete, "group", "participant-group");
        }, {students: true});

        // Ручная правка ФИО после выбора карточки: связь больше не
        // соответствует введённому имени — тихо сбрасываем (поля не трогаем).
        fioInput.addEventListener("input", () => {
            if (selectedStudent && fioInput.value !== selectedStudent.name) {
                setStudent(null);
            }
        });
        // «сбросить связь»: только связь, автоподставленные поля остаются.
        if (resetLink) {
            resetLink.addEventListener("click", () => setStudent(null));
        }
    }

    handleClick(event) {
        const editButton = event.target.closest(".participant-edit-button");
        if (editButton) {
            this.startEdit(editButton);
            return;
        }
        const saveButton = event.target.closest(".participant-save-button");
        if (saveButton) {
            this.saveEdit();
            return;
        }
        const cancelButton = event.target.closest(".participant-cancel-button");
        if (cancelButton) {
            this.cancelEdit();
            return;
        }
        const deleteButton = event.target.closest(".participant-delete-button");
        if (deleteButton) {
            this.deleteParticipant(
                deleteButton.dataset.recordId,
                deleteButton.dataset.studentName,
                deleteButton.dataset.discipline
            );
        }
    }

    buildTextInput(key, value, type = "text") {
        const input = document.createElement("input");
        input.type = type;
        input.className = "form-control form-control-sm";
        input.dataset.editKey = key;
        input.value = value ?? "";
        if (type === "number") {
            input.min = key === "course" ? "1" : "0";
            input.step = "1";
        }
        return input;
    }

    // Инлайн-правка строки участия (P7): и плоская строка, и child-строка
    // «└─» раскрываются в ПОЛНУЮ строку со всеми полями — снимок участника
    // (ФИО/пол/институт/группа/курс из данных строки/группы) редактируется
    // вместе с полями участия (дисциплина/место/результат). Сохранение —
    // тем же эндпоинтом, что и на главной (историчность — на сервере).
    startEdit(button) {
        if (this.addDirty) { notify({type: "error", text: "Сначала сохраните участника или отмените добавление."}); return; }
        this.setAddOpen(false);
        if (this.editingRow) {
            return;
        }
        const row = button.closest("tr");
        // Backup ДО любых манипуляций с ячейками: отмена (cancelEdit)
        // должна вернуть исходную разметку строки — включая «сжатую»
        // ведущую ячейку child-строки (colspan="6" с «└─»).
        this.editingBackup = row.innerHTML;
        this.editingRow = row;
        row.classList.add("event-row-editing");
        // Child-строка группы начинается с ячейки colspan="6" (поля
        // участника — в parent). Перед правкой раскрываем её в 1 + N-1
        // пустых <td>: фиксированные индексы ячеек ниже и row.children[1]
        // (ФИО) работают как у плоской строки.
        const firstCell = row.querySelector("td");
        if (firstCell && firstCell.colSpan > 1) {
            const span = firstCell.colSpan;
            firstCell.colSpan = 1;
            let anchor = firstCell;
            for (let index = 1; index < span; index += 1) {
                const filler = document.createElement("td");
                anchor.after(filler);
                anchor = filler;
            }
        }
        const cells = row.querySelectorAll("td");
        const dataset = button.dataset;
        // Ячейки: №, ФИО, Пол, Институт, Группа, Курс, Дисциплина, Место,
        // Результат, Действия
        const sexCell = cells[2];
        const instituteCell = cells[3];
        const groupCell = cells[4];
        const courseCell = cells[5];
        const disciplineCell = cells[6];
        const positionCell = cells[7];
        const resultCell = cells[8];
        const actionsCell = cells[cells.length - 1];

        const fioInput = this.buildTextInput("student_name", dataset.studentName);
        const sexInput = document.createElement("select");
        ["М", "Ж"].forEach((option) => {
            const item = document.createElement("option");
            item.value = option;
            item.textContent = option;
            sexInput.append(item);
        });
        sexInput.className = "form-control form-control-sm";
        sexInput.dataset.editKey = "student_sex";
        sexInput.value = dataset.studentSex || "М";

        let fieldSettings = {};
        try { fieldSettings = JSON.parse(this.table.dataset.baseFieldSettings || "{}"); } catch (error) { /* Legacy fallback. */ }
        const positionInput = this.buildTextInput("position", dataset.position, "text");
        positionInput.placeholder = "Место или DNF";
        const instituteInput = this.buildTextInput("institute", dataset.institute);
        const groupInput = this.buildTextInput("group", dataset.group);
        const disciplineInput = this.buildTextInput("discipline", dataset.discipline);
        const resultInput = this.buildTextInput("result", dataset.result);

        fioInput.required = true;
        [[positionInput, "position"], [instituteInput, "institute"], [groupInput, "group"]].forEach(([input, key]) => {
            input.required = Boolean(fieldSettings[key]?.required);
        });
        instituteCell.replaceChildren(instituteInput);
        groupCell.replaceChildren(groupInput);
        sexCell.replaceChildren(sexInput);
        courseCell.textContent = "Рассчитывается по группе и дате";
        positionCell.replaceChildren(positionInput);
        row.children[1].replaceChildren(fioInput);
        disciplineCell.replaceChildren(disciplineInput);
        resultCell.replaceChildren(resultInput);
        // Связь с карточкой студента — статичный бейдж под полем ФИО:
        // сохраняется при правке, редактировать её здесь нельзя (инструмент
        // смены связи — раздел «Сопоставление данных»).
        const studentRefId = String(dataset.studentRefId || "").trim();
        if (studentRefId) {
            const badge = document.createElement("span");
            badge.className = "badge text-bg-secondary participant-student-link";
            badge.textContent = "карточка студента";
            badge.title = "Связь с карточкой студента сохраняется при правке. Изменить её можно в разделе «Сопоставление данных».";
            const badgeWrap = document.createElement("div");
            badgeWrap.className = "mt-1";
            badgeWrap.append(badge);
            row.children[1].append(badgeWrap);
        }

        actionsCell.innerHTML = "";
        const saveButton = document.createElement("button");
        saveButton.type = "button";
        saveButton.className = "btn btn-sm btn-success participant-save-button";
        saveButton.textContent = "Сохранить";
        saveButton.title = "Сохранить";
        const cancelButton = document.createElement("button");
        cancelButton.type = "button";
        cancelButton.className = "btn btn-sm btn-outline-secondary participant-cancel-button";
        cancelButton.textContent = "Отмена";
        cancelButton.title = "Отмена";
        actionsCell.append(saveButton, cancelButton);

        // P7: participation-кастомы — sub-row под редактируемой строкой
        // (паттерн sub-row формы добавления): значения из data-атрибута
        // строки, имена custom__<key> — как у форм импорта/добавления.
        this.editingSubRow = this.buildCustomFieldsSubRow(row);

        this.activeFioResolver = new FioResolver(fioInput, (athlete) => {
            const mapping = {sex: sexInput, institute: instituteInput, group: groupInput};
            Object.entries(mapping).forEach(([athleteKey, input]) => {
                const value = athlete[athleteKey];
                if (value && String(input.value) !== String(value)) {
                    input.value = value;
                }
            });
        }, {students: true});

        this.keydownHandler = (keyEvent) => {
            // Открытый список резолвера работает в два шага (Enter — выбор,
            // Esc — закрытие): строку в это время не сохраняем и не отменяем.
            if (this.activeFioResolver && this.activeFioResolver.isOpen()) {
                return;
            }
            if (keyEvent.key === "Escape") {
                keyEvent.preventDefault();
                keyEvent.stopPropagation();
                this.cancelEdit();
            } else if (keyEvent.key === "Enter" && keyEvent.target.tagName !== "SELECT") {
                keyEvent.preventDefault();
                keyEvent.stopPropagation();
                this.saveEdit();
            }
        };
        // Capture-фаза: обработчик строки срабатывает раньше резолвера ФИО
        // (иначе его Enter/Esc в target-фазе съедали клавиши строки).
        row.addEventListener("keydown", this.keydownHandler, true);
        this.decorateParticipantCells();
        row.querySelectorAll("[data-edit-key]").forEach(input => {
            input.setAttribute("aria-label", input.closest("td")?.dataset.label || input.dataset.editKey);
        });
        resultInput.focus();
    }

    buildCustomFieldsSubRow(row) {
        if (!this.participationCustomFields.length) {
            return null;
        }
        let values = {};
        try {
            const parsed = JSON.parse(row.dataset.participationCustom || "{}");
            if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
                values = parsed;
            }
        } catch (error) {
            values = {};
        }
        const subRow = document.createElement("tr");
        subRow.className = "row-edit-participation-cf";
        const cell = document.createElement("td");
        cell.colSpan = row.children.length;
        const wrap = document.createElement("div");
        wrap.className = "d-flex flex-wrap gap-3 align-items-end py-2";
        this.participationCustomFields.forEach((field) => {
            const fieldWrap = document.createElement("div");
            const label = document.createElement("label");
            label.className = "form-label small mb-0";
            label.textContent = field.label;
            const input = this.buildTextInput(`custom__${field.key}`, values[field.key] ?? "");
            input.id = `participant-edit-custom-${field.key}`;
            label.htmlFor = input.id;
            fieldWrap.append(label, input);
            wrap.append(fieldWrap);
        });
        cell.append(wrap);
        subRow.append(cell);
        row.after(subRow);
        return subRow;
    }

    saveEdit() {
        if (!this.editingRow || this.participantSaving) {
            return;
        }
        const invalid = [...this.editingRow.querySelectorAll("input, select")].find(input => !input.checkValidity());
        if (invalid) { invalid.reportValidity(); return; }
        const recordId = this.editingRow.dataset.recordId;
        const formData = new FormData();
        this.editingRow.querySelectorAll("[data-edit-key]").forEach((input) => {
            formData.append(input.dataset.editKey, input.value);
        });
        // Participation-кастомы живут в sub-row (строка таблицы имеет
        // фиксированные колонки) — собираем и их.
        if (this.editingSubRow) {
            this.editingSubRow.querySelectorAll("[data-edit-key]").forEach((input) => {
                formData.append(input.dataset.editKey, input.value);
            });
        }
        // Поля события — из пресета таблицы: в записи они не редактируются.
        ["date", "level", "name", "sport"].forEach((key) => {
            formData.append(key, this.table.dataset[`event${key.charAt(0).toUpperCase()}${key.slice(1)}`] || "");
        });
        // Защита от повторного Enter и двойного клика по «✓» (тот же паттерн,
        // что у saveInlineEdit/saveNewRowEdit): один запрос на одно сохранение.
        this.participantSaving = true;
        this.editingRow.setAttribute("aria-busy", "true");
        this.editingRow.querySelectorAll("button, input, select").forEach(control => { control.disabled = true; });
        this.editingSubRow?.querySelectorAll("input").forEach(control => { control.disabled = true; });
        this.editingRow.querySelector(".participant-save-button").textContent = "Сохранение…";
        this.request(`/competition/${recordId}`, {
            body: formData,
            onSuccess: () => {
                this.participantSaving = false;
                // Выходим из правки ДО навигации: beforeunload-guard (UX P0
                // A3) блокирует уход со страницы, пока editingRow установлен,
                // — собственная навигация после сохранения не должна
                // упираться в собственный же guard.
                this.storeView(recordId);
                const position = formData.get("position");
                const disappeared = this.root.dataset.resultFilter === "without" && Number(position) !== 0
                    || this.root.dataset.resultFilter === "with" && Number(position) === 0;
                this.cancelEdit();
                this.navigateToEventWithMessage((disappeared ? "Участие обновлено. Строка скрыта текущим фильтром места." : "Участие обновлено") + (this.teamRemovalMessage ? " " + this.teamRemovalMessage : ""));
            },
            onError: (message) => {
                this.participantSaving = false;
                this.editingRow.removeAttribute("aria-busy");
                this.editingRow.querySelectorAll("button, input, select").forEach(control => { control.disabled = false; });
                this.editingSubRow?.querySelectorAll("input").forEach(control => { control.disabled = false; });
                this.editingRow.querySelector(".participant-save-button").textContent = "Сохранить";
                let error = this.editingRow.querySelector(".event-edit-error");
                if (!error) { error = document.createElement("div"); error.className = "event-edit-error"; error.setAttribute("role", "alert"); this.editingRow.lastElementChild.append(error); }
                error.textContent = message || "Ошибка сохранения. Введённые значения сохранены; повторите попытку.";
                notify({type: "error", text: message || "Ошибка сохранения записи"});
            }
        });
    }

    // Возврат на страницу события с flash-сообщением (server-side alert в
    // base.html): id события берём из адреса — разметку таблицы не трогаем.
    // Текущий result-фильтр страницы («С результатом»/«Без результата»)
    // сохраняется в возврате — после правки/удаления участия вид не сбрасывается.
    navigateToEventWithMessage(message) {
        const eventId = (window.location.pathname.match(/^\/calendar\/(\d+)/) || [])[1];
        if (!eventId) {
            window.location.reload();
            return;
        }
        const url = new URL(`/calendar/${eventId}`, location.origin);
        const returnContext = new URL(location.href).searchParams.get("return");
        if (returnContext) url.searchParams.set("return", returnContext);
        const resultFilter = new URLSearchParams(location.search).get("result");
        if (resultFilter) url.searchParams.set("result", resultFilter);
        if (this.search?.value) url.searchParams.set("q", this.search.value);
        if (this.activeTab && this.activeTab !== "participants") url.searchParams.set("tab", this.activeTab);
        url.searchParams.set("admin_message", message);
        window.location.href = url;
    }

    cancelEdit() {
        if (this.participantSaving) return;
        if (!this.editingRow) {
            return;
        }
        if (this.keydownHandler) {
            this.editingRow.removeEventListener("keydown", this.keydownHandler, true);
            this.keydownHandler = null;
        }
        if (this.activeFioResolver) {
            this.activeFioResolver.destroy();
            this.activeFioResolver = null;
        }
        if (this.editingSubRow) {
            this.editingSubRow.remove();
            this.editingSubRow = null;
        }
        const row = this.editingRow;
        this.editingRow.innerHTML = this.editingBackup;
        this.editingRow.classList.remove("event-row-editing");
        this.editingRow.removeAttribute("aria-busy");
        this.editingRow = null;
        this.editingBackup = null;
        this.decorateParticipantCells();
        row.querySelector(".participant-edit-button")?.focus({preventScroll: true});
        this.filterParticipants();
    }

    // Удаление участия из соревнования — это обычное удаление записи
    // реестра (с правами роли: эндпоинт админский, кнопка видна только
    // админу). После удаления страница перечитается, счётчики обновятся.
    // P7: подтверждение называет конкретное участие (ФИО — дисциплина);
    // у участия без дисциплины — «без дисциплины».
    deleteParticipant(recordId, studentName, discipline = "") {
        if (this.editingRow || this.addDirty) { notify({type: "error", text: "Перед удалением завершите или отмените текущий ввод."}); return; }
        const disciplineLabel = String(discipline || "").trim() || "без дисциплины";
        const message = studentName
            ? `Удалить участие «${studentName} — ${disciplineLabel}»? Будет удалена запись о соревновании вместе с результатом и вложениями — действие необратимо.`
            : "Удалить участие? Действие необратимо.";
        if (!confirm(message)) {
            return;
        }
        this.request(`/competition/${recordId}/delete`, {
            body: new FormData(),
            // Пользователь уже подтвердил удаление — если в этот момент
            // открыта правка любой строки, выходим из неё до навигации
            // (beforeunload-guard A3 не должен блокировать возврат на
            // страницу события; восстановление innerHTML безвредно — сразу
            // навигация). cancelEdit сам no-op без активной правки.
            onSuccess: () => {
                this.cancelEdit();
                this.navigateToEventWithMessage("Участие удалено");
            },
            onError: (message2) => notify({type: "error", text: message2 || "Ошибка удаления записи"}),
        });
    }
}

// Student Profile P2: progressive enhancement of existing native POST forms.
class StudentProfilePage {
    constructor(root) {
        this.root = root;
        this.tabs = [...root.querySelectorAll('[data-person-tab]')];
        this.search = root.querySelector('#person-history-search');
        this.dirty = new Set();
        this.busy = false;
        const params = new URL(location.href).searchParams;
        this.returnURL = this.safeReturn(params.get('return')) || this.safeReturn(document.referrer, true) || '/admin/people';
        const backLink = root.querySelector('[data-person-back]');
        backLink.href = this.returnURL;
        backLink.textContent = this.returnURL.split(/[?#]/)[0] === '/reports' ? '← К отчёту' : this.returnURL.startsWith('/calendar/') ? '← К соревнованию' : '← К студентам';
        root.classList.add('person-enhanced');
        root.querySelector('.person-tabs').setAttribute('role', 'tablist');
        this.tabs.forEach((tab, index) => {
            tab.id = `person-tab-${tab.dataset.personTab}`;
            tab.setAttribute('role', 'tab');
            tab.setAttribute('aria-controls', `person-${tab.dataset.personTab}`);
            tab.addEventListener('click', event => { event.preventDefault(); this.showTab(tab.dataset.personTab); });
            tab.addEventListener('keydown', event => {
                let next;
                if (event.key === 'ArrowRight') next = (index + 1) % this.tabs.length;
                if (event.key === 'ArrowLeft') next = (index + this.tabs.length - 1) % this.tabs.length;
                if (event.key === 'Home') next = 0;
                if (event.key === 'End') next = this.tabs.length - 1;
                if (next !== undefined) { event.preventDefault(); if (this.showTab(this.tabs[next].dataset.personTab)) this.tabs[next].focus(); }
            });
        });
        root.querySelectorAll('[data-person-panel]').forEach(panel => {
            panel.setAttribute('role', 'tabpanel');
            panel.setAttribute('aria-labelledby', `person-tab-${panel.dataset.personPanel}`);
        });
        this.search.value = (params.get('history_q') || '').slice(0, 2000);
        this.showTab(params.get('tab') || location.hash.replace('#person-', '') || 'history', false);
        this.search.addEventListener('input', () => this.filter());
        root.querySelectorAll('[data-person-clear]').forEach(button => button.addEventListener('click', () => { this.search.value = ''; this.filter(); this.search.focus(); }));
        root.querySelector('[data-person-edit]')?.addEventListener('click', () => {
            if (!this.showTab('data')) return;
            root.querySelector('#person-edit-details').open = true;
            root.querySelector('#person-full-name').focus();
        });
        root.querySelectorAll('form[method="post"]').forEach(form => {
            form.addEventListener('input', () => this.dirty.add(form));
            form.addEventListener('change', () => this.dirty.add(form));
            if (form.querySelector('input:not([type=hidden]), select')) {
                const cancel = document.createElement('button');
                cancel.type = 'button'; cancel.className = 'btn btn-outline-secondary person-cancel'; cancel.textContent = 'Отменить';
                cancel.addEventListener('click', () => {
                    if (this.busy) return;
                    form.reset(); this.dirty.delete(form); form.querySelector('.person-form-error')?.remove();
                    const details = form.closest('details'); if (details) { details.open = false; details.querySelector('summary').focus(); }
                });
                form.append(cancel);
            }
            form.addEventListener('submit', event => {
                if (event.defaultPrevented) return;
                event.preventDefault(); this.submit(form);
            });
        });
        window.addEventListener('beforeunload', event => {
            if (this.dirty.size || this.busy) { event.preventDefault(); event.returnValue = ''; }
        });
        root.querySelectorAll('table:not(.person-history-table)').forEach(table => {
            table.classList.add('person-secondary-table');
            const labels = [...table.querySelectorAll('thead th')].map(cell => cell.textContent.trim());
            table.querySelectorAll('tbody tr').forEach(row => [...row.cells].forEach((cell, index) => { cell.dataset.label = labels[index] || ''; }));
        });
        this.filter();
        this.viewKey = `competitions:person-view:${root.dataset.personPage}`;
        try {
            const view = JSON.parse(sessionStorage.getItem(this.viewKey) || 'null');
            sessionStorage.removeItem(this.viewKey);
            if (view && Date.now() - view.at >= 0 && Date.now() - view.at < 60000) requestAnimationFrame(() => {
                const target = view.focusId ? document.getElementById(view.focusId) : this.tabs.find(tab => tab.dataset.personTab === this.activeTab);
                target?.focus({preventScroll: true}); window.scrollTo(0, Number(view.scrollY) || 0);
            });
        } catch (error) { /* Optional viewport context; never form values. */ }
    }
    safeReturn(value, allowAbsolute = false) {
        if (!value) return null;
        try {
            let decoded = value;
            for (let i = 0; i < 4; i++) {
                if (/[\\\x00-\x1f\x7f]/.test(decoded)) return null;
                let next;
                try { next = decodeURIComponent(decoded); } catch (error) { break; }
                if (next === decoded) break;
                decoded = next;
            }
            if (!value.startsWith('/') && !allowAbsolute) return null;
            if (value.startsWith('//')) return null;
            const url = new URL(value, location.origin);
            const rawPath = value.startsWith('/') ? value.split(/[?#]/)[0] : url.pathname;
            if (url.origin !== location.origin || !(/^\/admin\/people$/.test(rawPath) || /^\/calendar\/[0-9]+$/.test(rawPath) || rawPath === '/reports')) return null;
            url.searchParams.delete('admin_message'); url.searchParams.delete('admin_error');
            return url.pathname + url.search + url.hash;
        } catch (error) { return null; }
    }
    showTab(key, guard = true) {
        if (guard && (this.dirty.size || this.busy)) { notify({type: 'error', text: 'Сохраните или отмените введённые изменения перед переходом.'}); return false; }
        if (!this.tabs.some(tab => tab.dataset.personTab === key)) key = 'history';
        this.activeTab = key;
        this.tabs.forEach(tab => { const active = tab.dataset.personTab === key; tab.setAttribute('aria-selected', String(active)); tab.tabIndex = active ? 0 : -1; });
        this.root.querySelectorAll('[data-person-panel]').forEach(panel => { panel.hidden = panel.dataset.personPanel !== key; });
        this.updateURL(); return true;
    }
    updateURL() {
        const url = new URL(location.href);
        url.searchParams.set('tab', this.activeTab);
        if (this.search.value) url.searchParams.set('history_q', this.search.value); else url.searchParams.delete('history_q');
        url.searchParams.set('return', this.returnURL); url.hash = '';
        history.replaceState(null, '', url);
    }
    filter() {
        const query = this.search.value.trim().toLocaleLowerCase('ru');
        const rows = [...this.root.querySelectorAll('[data-history-record]')];
        let found = 0;
        rows.forEach(row => { row.hidden = !row.dataset.historySearch.toLocaleLowerCase('ru').includes(query); if (!row.hidden) found++; });
        this.root.querySelector('[data-person-found]').textContent = `Найдено ${found} из ${rows.length} загруженных участий`;
        this.root.querySelector('[data-person-no-results]').hidden = !rows.length || found > 0;
        this.updateURL();
    }
    async submit(form) {
        if (this.busy) return;
        if ([...this.dirty].some(other => other !== form)) { notify({type: 'error', text: 'Сначала сохраните или отмените изменения в другой форме.'}); return; }
        const body = new FormData(form);
        const controls = [...form.querySelectorAll('button, input, select')];
        const states = controls.map(control => control.disabled);
        this.busy = true; form.setAttribute('aria-busy', 'true');
        controls.forEach(control => { control.disabled = true; });
        form.querySelector('.person-form-error')?.remove();
        try {
            const response = await fetch(form.action, {method: 'POST', body, credentials: 'same-origin'});
            const target = new URL(response.url);
            if (target.pathname === '/login' || response.status === 401) throw new Error('Сессия завершилась. Введённые данные оставлены в форме; войдите в приложение в другой вкладке и повторите сохранение.');
            if (target.searchParams.has('admin_error')) throw new Error(target.searchParams.get('admin_error'));
            if (!response.ok) throw new Error(`Не удалось сохранить данные (HTTP ${response.status}). Повторите попытку.`);
            this.dirty.delete(form); this.busy = false;
            if (target.pathname === location.pathname) {
                const currentParams = new URL(location.href).searchParams;
                ['period', 'date_from', 'date_to', 'offset'].forEach(key => {
                    if (currentParams.has(key)) target.searchParams.set(key, currentParams.get(key));
                });
                target.searchParams.set('tab', this.activeTab);
                if (this.search.value) target.searchParams.set('history_q', this.search.value);
                target.searchParams.set('return', this.returnURL);
                try { sessionStorage.setItem(this.viewKey, JSON.stringify({at: Date.now(), scrollY: window.scrollY, focusId: `person-tab-${this.activeTab}`})); }
                catch (error) { /* Storage is optional. */ }
            }
            location.assign(target.href);
        } catch (error) {
            this.busy = false;
            if (form.querySelector('input:not([type=hidden]), select')) this.dirty.add(form);
            const alert = document.createElement('div'); alert.className = 'person-form-error'; alert.setAttribute('role', 'alert'); alert.tabIndex = -1;
            alert.textContent = error.message || 'Ошибка соединения. Введённые данные сохранены в форме.';
            form.append(alert); alert.focus();
        } finally {
            form.removeAttribute('aria-busy'); controls.forEach((control, index) => { control.disabled = states[index]; });
        }
    }
}


// Keep the native select as the submitted value. The input only searches;
// arbitrary search text never becomes a level. Existing portalled dropdown
// remains visible outside responsive tables, in both themes.
function enhanceLevelSelect(select) {
    const wrapper = document.createElement("div");
    wrapper.className = "input-group inline-combobox-group";
    const input = document.createElement("input");
    input.type = "text";
    input.className = select.className.replace(/form-select/g, "form-control");
    input.autocomplete = "off";
    input.value = select.value;
    input.placeholder = "Выберите уровень";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "btn btn-outline-secondary combobox-toggle";
    toggle.textContent = "▾";
    toggle.setAttribute("aria-label", "Показать уровни");
    if (select.id) {
        input.id = select.id;
        select.removeAttribute("id");
    }
    const combo = new InlineComboBox(input, toggle, () => Array.from(select.options).filter(option => !option.hasAttribute("data-level-unavailable")).map(option => option.value));
    const validateSelection = () => input.setCustomValidity(
        input.value !== select.value || select.selectedOptions[0]?.hasAttribute("data-level-unavailable")
            ? "Выберите уровень из списка" : ""
    );
    validateSelection();
    combo.dropdown.classList.add("combobox-dropdown--strict-level");
    combo.dropdown.setAttribute("role", "listbox");
    combo.dropdown.id = `level-options-${Math.random().toString(36).slice(2)}`;
    input.setAttribute("aria-controls", combo.dropdown.id);
    combo.positionDropdown = () => {
        if (!combo.open) return;
        const rect = input.getBoundingClientRect();
        const below = window.innerHeight - rect.bottom - 8;
        const above = rect.top - 8;
        const upwards = below < 180 && above > below;
        const available = Math.max(40, upwards ? above : below);
        combo.dropdown.style.maxHeight = `${Math.min(260, available)}px`;
        combo.dropdown.style.minWidth = "0";
        combo.dropdown.style.width = `${Math.min(rect.width + toggle.offsetWidth, window.innerWidth - 16)}px`;
        combo.dropdown.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - combo.dropdown.offsetWidth - 8))}px`;
        combo.dropdown.style.top = `${upwards ? rect.top - combo.dropdown.offsetHeight - 2 : rect.bottom + 2}px`;
    };
    combo.select = value => {
        select.value = value;
        input.value = value;
        input.setCustomValidity("");
        combo.close();
        select.dispatchEvent(new Event("change", {bubbles: true}));
    };
    const renderOptions = combo.renderOptions.bind(combo);
    combo.renderOptions = () => {
        renderOptions();
        combo.dropdown.querySelectorAll(".combobox-empty").forEach(item => {
            item.textContent = "Нет совпадений. Добавить уровень можно в административном справочнике.";
        });
        combo.dropdown.querySelectorAll('.combobox-option[data-value=""]').forEach(item => { item.textContent = "Не указан"; });
    };
    const openDropdown = combo.openDropdown.bind(combo);
    combo.openDropdown = () => { openDropdown(); input.setAttribute("aria-expanded", "true"); };
    const close = combo.close.bind(combo);
    combo.close = () => { close(); input.setAttribute("aria-expanded", "false"); };
    input.addEventListener("input", () => {
        validateSelection();
    });
    input.addEventListener("blur", () => {
        setTimeout(() => { input.value = select.value; validateSelection(); }, 150);
    });
    select.classList.add("d-none");
    select.tabIndex = -1;
    wrapper.append(input, toggle, select);
    return {wrapper, combo};
}

function initializeLevelSelects() {
    document.querySelectorAll("select[data-strict-level]").forEach(select => {
        const parent = select.parentNode;
        const next = select.nextSibling;
        const strict = enhanceLevelSelect(select);
        parent.insertBefore(strict.wrapper, next);
        const form = select.form;
        if (form && !form.dataset.levelPreserve) {
            form.dataset.levelPreserve = "1";
            form.addEventListener("submit", async event => {
                if (event.defaultPrevented) return;
                event.preventDefault();
                if (form.dataset.levelSubmitting) return;
                form.dataset.levelSubmitting = "1";
                try {
                    const response = await fetch(form.action, {method: "POST", body: new FormData(form)});
                    if (!response.ok) {
                        notify({type: "error", text: await response.text()});
                        return;
                    }
                    window.location.assign(response.url);
                } catch (_) {
                    notify({type: "error", text: "Не удалось сохранить. Введённые данные сохранены в форме."});
                } finally {
                    delete form.dataset.levelSubmitting;
                }
            });
        }
    });
}
window.addEventListener("DOMContentLoaded", initializeLevelSelects);
