#!/usr/bin/env bash
# Ускоренный прогон pytest: временные каталоги тестов (basetemp) размещаются
# в tmpfs (/dev/shm), а не на диске.
#
# Зачем: все временные файлы тестов Competitions создаются через фикстуру
# pytest tmp_path (полный прогон создаёт ~600 SQLite-баз), и на диске создание
# схемы заметно медленнее, чем в tmpfs. Прогон на /dev/shm существенно быстрее.
#
# Ловушка, из-за которой этот скрипт существует: если каталог --basetemp уже
# существует, pytest при старте стирает его ЦЕЛИКОМ и создаёт заново
# (_pytest/tmpdir.py). Поэтому --basetemp можно указывать только на свежий
# уникальный каталог: его нельзя вынести в pytest-конфиг, а передавать вручную
# на существующий путь опасно. Скрипт создаёт каталог через mktemp (атомарно,
# безопасно при параллельных прогонах) и удаляет его по завершении.
#
# Аргументы скрипта пробрасываются в pytest без изменений, например:
#   ./scripts/pytest_tmpfs.sh src/ -q
#   ./scripts/pytest_tmpfs.sh src/storage_test.py -q
#
# Если /dev/shm недоступен (нет tmpfs, мало места) — прогоняйте тесты обычным
# способом: ./venv/bin/python -m pytest src/ -q

set -euo pipefail

# Минимум свободного места в /dev/shm (МБ). Пик занятого места в basetemp
# за полный прогон по двум независимым замерам — 74–93 МБ (октябрь 2026,
# 1034 теста); 512 МБ — 5–7-кратный запас на рост набора тестов.
MIN_FREE_MB=512

# Корень репозитория определяем от расположения скрипта — запуск из любого cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

PY="$REPO_ROOT/venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "Ошибка: не найден $PY." >&2
  echo "Создайте виртуальное окружение по README.md:" >&2
  echo "  python -m venv venv && ./venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [ ! -d /dev/shm ] || [ ! -w /dev/shm ]; then
  echo "Ошибка: /dev/shm не существует или недоступен на запись — tmpfs-прогон невозможен." >&2
  echo "Запустите тесты обычным способом: ./venv/bin/python -m pytest src/ -q" >&2
  exit 1
fi

# df -Pm: POSIX-формат, размеры в МБ; поле 4 второй строки — свободно.
# Если df неожиданно упал, показываем свою подсказку, а не молча выходим.
if ! free_mb="$(df -Pm /dev/shm | awk 'NR==2 {print $4}')" \
   || [ -z "$free_mb" ] || [ "$free_mb" -lt "$MIN_FREE_MB" ]; then
  echo "Ошибка: в /dev/shm свободно меньше ${MIN_FREE_MB} МБ (сейчас: ${free_mb:-неизвестно} МБ)." >&2
  echo "Проверьте df -h /dev/shm или запустите тесты обычным способом: ./venv/bin/python -m pytest src/ -q" >&2
  exit 1
fi

# basetemp управляет только скрипт: pytest применяет последний переданный
# вариант и по last-wins стёр бы пользовательский каталог — запрещаем свой.
for arg in "$@"; do
  case "$arg" in
    --basetemp|--basetemp=*)
      echo "Ошибка: не передавайте --basetemp — скрипт сам управляет временным каталогом." >&2
      echo "Для ручного управления basetemp используйте обычный ./venv/bin/python -m pytest." >&2
      exit 1
      ;;
  esac
done

# Свежий уникальный каталог: pytest стирает существующий --basetemp целиком,
# поэтому единственный безопасный вариант — только что созданный mktemp'ом.
BASETMP="$(mktemp -d /dev/shm/competitions-pytest.XXXXXXXX)"

# Очистка: удаляем ТОЛЬКО наш каталог, без шаблонов по /dev/shm.
cleanup() {
  rm -rf -- "$BASETMP"
}
trap cleanup EXIT
trap 'exit 130' INT   # SIGINT: выходим, EXIT-ловушка удалит каталог
trap 'exit 143' TERM  # SIGTERM: аналогично

echo "tmpfs basetemp: $BASETMP"

# Код выхода скрипта совпадает с кодом выхода pytest;
# очистка срабатывает в любом случае (EXIT-ловушка).
set +e
"$PY" -m pytest --basetemp="$BASETMP" "$@"
status=$?
set -e
exit "$status"
