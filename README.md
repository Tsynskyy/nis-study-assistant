# Учебный ассистент

RAG-система, которая отвечает на вопросы о курсах, дедлайнах и правилах оценивания по выгрузкам из Smart LMS, Т-Образования и учебных сводок

| ДЗ | Тема | Файл |
| --- | --- | --- |
| 1 | Постановка задачи | [docs/hw1-problem.md](docs/hw1-problem.md) |
| 2 | Анализ вариантов и план реализации | [docs/hw2-plan.md](docs/hw2-plan.md) |
| 3 | Проверка подходов, MVP и оценка качества | [docs/hw3-mvp.md](docs/hw3-mvp.md) |
| 4 | Архитектура и пайплайны | [docs/hw4-production.md](docs/hw4-production.md) |
| 5 | Эксплуатации и мониторинг | [docs/hw5-operations.md](docs/hw5-operations.md) |

## Запуск

Python 3.11+ без сторонних библиотек
Ассистент читает выгрузки из HSE_PASSPORT_DIR

```bash
export HSE_PASSPORT_DIR=../hse/student-passport
python3 -m assistant ask "Какие ближайшие дедлайны?"
python3 -m assistant chat      # диалог
python3 -m assistant eval      # оценка качества
python3 -m assistant report    # мониторинг
python3 -m unittest discover -s tests -q
```

## Локальная модель

Ответ пишет локальная модель в Ollama, по умолчанию `gemma4:e4b`
Если Ollama не запущена, ассистент показывает найденный фрагмент без модели

```bash
ollama pull gemma4:e4b
python3 -m assistant ask "Сколько баллов можно набрать по БКС?"
python3 -m assistant ask "..." --model qwen2.5:7b   # другая модель
python3 -m assistant ask "..." --no-llm             # без модели
```

## Почта и календарь

Пароли лежат в `.env`
Пароли приложений создаются в Яндекс ID

```
SMTP_USER=student@edu.hse.ru
SMTP_PASSWORD=пароль приложения для почты
CALDAV_PASSWORD=пароль приложения для календаря
```

```bash
python3 -m assistant digest --dry-run     # посмотреть письмо
python3 -m assistant digest               # отправить дайджест сейчас
python3 -m assistant sync-calendar        # обновить Яндекс Календарь
python3 -m assistant schedule install     # дайджест в 09:00, календарь каждые 30 минут
python3 -m assistant schedule remove      # убрать расписание
```

В календарь попадают только новые и измененные сроки, каждый с напоминанием за сутки
Расписание ставится в планировщик Windows, на Linux и macOS команда печатает команды для cron

## MCP

`python3 -m assistant mcp` подключает ассистента к любому MCP-клиенту, например к LM Studio с локальной моделью
Модель получает три инструмента: поиск по материалам, дедлайны и отчет мониторинга
Отправлять письма модель не может

```json
{
  "mcpServers": {
    "study-assistant": {
      "command": "python",
      "args": ["-m", "assistant", "mcp"],
      "env": {"PYTHONPATH": "C:\\dev\\personal\\hse\\nis-study-assistant"}
    }
  }
}
```

## Устройство

- `assistant/corpus.py` - загрузка выгрузок и нарезка на фрагменты
- `assistant/retrieval.py` - три варианта поиска: по словам, TF-IDF, BM25
- `assistant/pipeline.py` - выбор маршрута, дедлайны, генерация, журнал
- `assistant/evaluate.py` - оценка качества
- `assistant/monitor.py` - мониторинг и алерты
- `assistant/integrations.py` - письмо-дайджест и события календаря
- `assistant/caldav.py` - запись событий в Яндекс Календарь
- `assistant/schedule.py` - `.env` и расписание
- `assistant/mcp.py` - MCP-сервер