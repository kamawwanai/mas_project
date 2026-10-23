# Безопасное подключение OpenRouter

Проверка выполнена 22 октября 2026 года по официальной документации OpenRouter.
Реальных запросов к модели при подготовке конфигурации не выполнялось.

## Модель и транспорт

| Параметр | Значение |
| --- | --- |
| Провайдер | `openrouter` |
| Model ID | `openai/gpt-4.1-mini` |
| Endpoint | `https://openrouter.ai/api/v1` |
| Транспорт | OpenAI-compatible Chat Completions |
| Endpoint-провайдер | `openai`, без fallback |
| Temperature | `0.0` |
| Максимальный выход вызова | задаётся общим runtime; для пилота `8000` токенов |
| Таймаут вызова | `120` секунд, но не больше остатка общего лимита времени |
| Повторы | до 3 попыток только при явном rate limit; пауза 1 секунда |

Страница модели подтверждает доступность `openai/gpt-4.1-mini`, tool calling и
structured output. Формат инструментов — OpenAI Chat Completions: объект `function`
в массиве `tools`, `tool_choice`, затем сообщения `assistant.tool_calls` и `role=tool`.
Для JSON Schema используется `response_format`. Маршрутизация включает
`require_parameters=true`, поэтому OpenRouter не должен выбирать endpoint без поддержки
переданных параметров.

Официальные источники:

- [карточка модели и тарифы](https://openrouter.ai/openai/gpt-4.1-mini);
- [OpenRouter Quickstart](https://openrouter.ai/docs/quickstart);
- [интеграция OpenAI SDK](https://openrouter.ai/docs/guides/community/openai-sdk);
- [tool calling](https://openrouter.ai/docs/guides/features/tool-calling);
- [structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs);
- [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection);
- [spend controls](https://openrouter.ai/docs/guides/best-practices/spend-controls).

## Тарифы

В `configs/pricing.json` зафиксированы тарифы стандартного endpoint на дату проверки:

| Единица | Цена, USD за 1 млн токенов |
| --- | ---: |
| Input | 0.40 |
| Cached input | 0.10 |
| Output | 1.60 |

Перед реальным запуском код проверяет совпадение провайдера и model ID, единицу
`tokens_per_million`, валюту USD, официальный URL источника и свежесть даты не более
семи дней. Устаревшая или неполная запись блокирует запуск. OpenRouter может изменить
тарифы, поэтому перед пилотом нужно повторно сверить карточку модели и обновить дату
только после фактической проверки.

## Ключ и два разрешающих барьера

1. Скопировать `.env.example` в `.env` и вставить ключ после
   `OPENROUTER_API_KEY=`. Файл `.env` игнорируется Git.
2. Перед запуском загрузить значение в окружение процесса, не печатая его. Проект
   намеренно не добавляет dotenv-загрузчик и не читает `.env` сам.
3. После явного разрешения на первый API-вызов установить `api_enabled = true` в
   `configs/llm.toml`.
4. Передать `--allow-api` конкретной команде. Без любого из двух разрешений сетевой
   вызов не начинается.

Пример загрузки локального `.env` в текущую PowerShell-сессию:

```powershell
Copy-Item .env.example .env
# Отредактировать .env локально, затем:
$line = Get-Content .env | Where-Object { $_ -like 'OPENROUTER_API_KEY=*' } | Select-Object -First 1
$env:OPENROUTER_API_KEY = $line.Substring('OPENROUTER_API_KEY='.Length)
```

Не выводите `$line` или `$env:OPENROUTER_API_KEY` в консоль и логи.

## Лимиты и безопасная остановка

Для первого полного повтора установлены `$0.50` на одну архитектуру и `$0.60`
на серию из трёх архитектур. Те же пределы записаны в `configs/pilot.example.toml`.
Дополнительно следует задать guardrail ключа OpenRouter не выше `$0.60`: локальный
лимит и лимит провайдера защищают разные уровни.

Перед каждым запросом runtime резервирует стоимость входа и максимально возможного
выхода. Неизвестный или некорректный usage, неизвестная стоимость, таймаут, ошибка
соединения или сервера останавливают запуск без бесконтрольных повторов. Повтор разрешён
только для явного rate limit и также учитывается в общем лимите запросов и времени.

После разрешения безопасный smoke-test выполняется командой:

```powershell
python -B scripts/smoke_openrouter.py --allow-api
```

Он допускает ровно одну попытку, максимум 128 выходных токенов и резерв `$0.01`.
Smoke-test просит модель сформировать один вызов локального инструмента, проверяет его
структуру и не выполняет инструмент или второй запрос.
