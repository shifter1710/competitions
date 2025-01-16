class Main {
    constructor() {
        this.contentWrapper = document.querySelector(".content-wrapper");
        this.importForm = document.querySelector(".import-form");
        this.reportForm = document.querySelector(".filter-form");
        this.fileInput = document.querySelector(".import-form__input");
        this.importButton = document.querySelector(".import-form__button");

        this.loader = document.querySelector(".loader");

        this.inputName = document.querySelector(".filter__name");
        this.dateFromInput = document.querySelector(".filter__date-from");
        this.dateToInput = document.querySelector(".filter__date-to");
        this.levelSelectWrapper = document.querySelector(".filter__level-wrapper")
        this.positionSelectWrapper = document.querySelector(".filter__position-wrapper")
        this.filterButton = document.querySelector(".filter__button");
        this.cleanFilterButton = document.querySelector(".filter-form__clean-button");
        this.cleanButton = document.querySelector(".clean-button");
        this.exportButton = document.querySelector(".export-button");
        this.dateRangePickerElement = document.querySelector('.datetime');

        this.filterIsApplied = false;
        this.createInstances();
        this.hangEvents();
    }

    createInstances() {
        this.dateRangePicker = new DateRangePicker(this.dateRangePickerElement, {
            format: "dd.mm.yyyy"
        });
        this.levelSelect = NiceSelect.bind(document.querySelector(".filter__level"), {searchable: true, searchtext: "Найти"});
        this.positionSelect = NiceSelect.bind(document.querySelector(".filter__position"), {searchable: true, searchtext: "Найти"});
    }

    destroyInstances() {
        this.levelSelect.destroy();
        this.positionSelect.destroy();
    }

    hangEvents() {
        this.importForm.addEventListener("submit", (event) => this.handleSubmitImportForm(event));
        this.fileInput.addEventListener("change", () => this.setDisabledImportButton(false))
        this.reportForm.addEventListener("submit", (event) => this.handleSubmitReportForm(event));
        this.cleanFilterButton.addEventListener("click", () => this.handleResetFilterButton())
        this.exportButton.addEventListener("click", () => this.handleClickExportButton())
        this.cleanButton.addEventListener("click", () => this.cleanDb());
    }

    setDisabledImportButton(state) {
        this.importButton.disabled = state;
    }

    cleanFileInput() {
        this.setDisabledImportButton(true);
        this.fileInput.value = "";
    }

    handleClickExportButton() {
        if (this.filterIsApplied) {
            const params = this.prepareParamsForReport();
            return this.makeExportRequest("/export/report?" + params);
        }
        this.makeExportRequest("/export/index");
    }

    handleResetFilterButton() {
        this.resetFilter();
        this.makeRequest({
            url: "/",
            options: {
                method: "GET",
            },
            onSuccess: () => {
                this.filterIsApplied = false;
            }
        })
    }

    resetFilter() {
        this.inputName.value = "";
        this.levelSelectWrapper.querySelector('li.option[data-value=""]').click();
        this.positionSelectWrapper.querySelector('li.option[data-value=""]').click();
        this.destroyInstances();
        this.createInstances();
        this.dateFromInput.value = "";
        this.dateToInput.value = "";
    }

    handleSubmitImportForm(event) {
        event.preventDefault();
        const formData = new FormData(this.importForm);
        this.importFile(formData);
    }

    prepareParamsForReport() {
        const params = new URLSearchParams();
        const formData = new FormData(this.reportForm);
        Array.from(formData.entries()).forEach(field => {
            const [fieldName, value] = field;
            if (value) {
                params.append(fieldName, value);
            }
        })
        return params.toString();
    }

    handleSubmitReportForm(event) {
        event.preventDefault();
        this.getReport(this.prepareParamsForReport());
    }

    importFile(formData) {
        this.makeRequest({
            url: "/",
            options: {
                method: "POST",
                body: formData,
            },
            onSuccess: () => {
                alert("Файл успешно импортирован");
                this.cleanFileInput();
                this.resetFilter();
                this.filterIsApplied = false;
            },
            onError: () => alert("Ошибка импорта")
        })
    }

    getReport(params) {
        this.makeRequest({
            url: "/report?" + params,
            options: {
                method: "GET"
            },
            onSuccess: () => this.filterIsApplied = true,
            onError: () => alert("Ошибка применения фильтра. Попробуйте позже")
        })
    }

    cleanDb() {
        const result = confirm("Вы действительно хотите очистить базу данных?");
        if (!result) {
            return;
        }
        this.makeRequest({
            url: "/clean_db",
            onSuccess: () => alert("База данных успешно очищена"),
            onError: () => alert("Ошибка очистки базы данных")
        })
    }

    makeRequest({ url, options = {}, onSuccess = () => {}, onError }) {
        this.setLoading(true)
        fetch(url, options)
        .then((response) => response.text())
        .then(html => {
            const parser = new DOMParser();
            const doc = parser.parseFromString(html, "text/html");
            const newContentWrapper = doc.querySelector(".content-wrapper");
            this.contentWrapper.innerHTML = newContentWrapper.innerHTML;
            onSuccess();
        })
        .catch(() => onError())
        .finally(() => this.setLoading(false))
    }

    makeExportRequest(url) {
        const linkElement = document.createElement("a");
        linkElement.href = url;
        linkElement.click();
    }

    setLoading(state) {
        if (state) {
            return this.loader.classList.add("loader-active");
        }
        this.loader.classList.remove("loader-active");
    }
}

new Main();
