# Ice.agent

Мультиагентная платформа для Telegram: несколько ИИ-агентов (Telethon user-аккаунты) с памятью Mem0, LLM (DeepSeek / GPT-5.5), веб-поиском, cron-задачами, MCP-интеграцией и веб-панелью управления.

## Возможности

- **Мультиагентность** — в панели создаётся любое число агентов, каждый со своим Telegram-аккаунтом, промптом, моделью и набором инструментов.
- **Межагентное взаимодействие** — агенты (по настроенным связям) могут уведомлять друг друга, задавать вопросы и ставить задачи.
- **Глубокая интеграция Telethon** — сообщения, диалоги, история, медиа, реакции, участники, каналы + автогенерируемая поверхность инструментов.
- **Эмуляция человека** — «печатает…», задержки пропорционально длине текста, разбивка длинных ответов.
- **Память Mem0** — долговременная память по каждому собеседнику и агенту.
- **Контекст переписки** — отдельная история по агенту, Telegram-чату и
  пользователю; точные даты сообщений, время последнего контакта и rolling
  summary для длинных диалогов.
- **Администратор системы** — Telegram user_id админа задаётся в настройках; агенты умеют эскалировать и принимать команды только от него.
- **Веб-поиск** — Tavily / SearXNG / DuckDuckGo (выбирается в Runtime).
- **Браузер Playwright MCP** — локальный headless Chromium для открытия найденных ссылок, кликов и извлечения данных.
- **Cron** — периодические задачи агентов (APScheduler).
- **MCP** — подключение внешних MCP-серверов (stdio / SSE / streamable-http) как инструментов агента.

## Быстрый старт (dev)

```bash
# backend
cd backend
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -e .
uvicorn app.main:app --reload

# frontend (в другом терминале)
cd frontend
npm install
npm run dev
```

Панель: http://localhost:5173, API/Swagger: http://localhost:8000/docs.

## Docker Compose

Стек изолирован (`name: iceagent`, своя сеть `iceagent_net`, volume с префиксом
`iceagent_*`). На multi-app сервере порты только на localhost и не пересекаются
с PokerClub / UralTrade / IceSchool / AYS Tracker / SearXNG / RustDesk:

| Сервис | Host bind | Зачем |
|--------|-----------|--------|
| UI | `0.0.0.0:3040` | панель (LAN + localhost; не 3000/3010/3090) |
| API | `0.0.0.0:8040` | прямой доступ к API при отладке |
| Playwright MCP | `127.0.0.1:8931` | локальный браузер для агентов |
| Postgres / Qdrant | нет publish | только внутренняя сеть |

```bash
cp .env.example .env
# задайте ICE_SECRET_KEY и ICE_ADMIN_PASSWORD
docker compose up -d --build
```

Если `npm ci` / `pip` в Docker падают с `ETIMEDOUT`, в `.env` можно задать прокси или зеркало:

```bash
# HTTP_PROXY=http://user:pass@host:8080
# HTTPS_PROXY=http://user:pass@host:8080
# NPM_REGISTRY=https://registry.npmmirror.com   # уже по умолчанию для UI
```

Панель: http://SERVER_IP:3040 (например http://192.168.10.64:3040)  
API: http://SERVER_IP:8040/docs  

Если из LAN не открывается — проверьте firewall:

```bash
sudo ufw allow 3040/tcp
sudo ufw reload
```

На сервере уже есть SearXNG (`:8080`). В Runtime → Веб-поиск можно выбрать:

- **Tavily** — основной рекомендуемый поиск (API-ключ с https://tavily.com, шифруется в БД; при 403 укажите HTTP-прокси);
- **SearXNG** — URL, например `http://172.17.0.1:8080` (docker0 / host gateway);
- **DuckDuckGo** — без внешней настройки.

Qdrant в compose: `http://qdrant:6333` (имя сервиса внутри сети).

Для чтения страниц по ссылкам из результатов поиска используйте Playwright MCP (ниже), а не веб-поиск.

Playwright MCP поднимается вместе со стеком. Если контейнер в `Restarting`, проверьте:
`docker logs iceagent-playwright --tail 50` — entrypoint должен быть `node /app/cli.js`.

Если в панели ошибка `MCPError: Server returned an error response` — это отказ по `Host`
(DNS rebinding). В compose уже стоит `--allowed-hosts *`.

В панели **MCP** добавьте сервер:

- имя: `playwright`
- транспорт: `streamable-http`
- URL: `http://playwright:8931/mcp` (из контейнера API)

У агента включите инструмент **MCP**. После этого агент сможет открывать сайты, делать snapshot страницы и извлекать данные через Playwright.

В production замените `ICE_SECRET_KEY` и `ICE_ADMIN_PASSWORD`. Это единственные
bootstrap-секреты: рабочие ключи настраиваются в веб-панели и шифруются в БД.

## Первоначальная настройка

1. Войдите в панель паролем из `ICE_ADMIN_PASSWORD`.
2. В разделе Providers создайте отдельные профили OpenAI/DeepSeek с ключами.
3. Добавьте Telegram-аккаунты: для каждого укажите собственные `api_id`,
   `api_hash` и телефон, затем подтвердите вход.
4. В Runtime settings настройте Mem0/Qdrant, поиск и параметры human typing.
   Там же выберите часовой пояс, глубину Telegram-истории и порог суммаризации.
5. Создайте агентов и каждому выберите свой LLM-профиль и Telegram-аккаунт.
6. Укажите Telegram user ID администраторов, связи агентов, cron и MCP.

Для четырёх агентов можно создать четыре независимых LLM-профиля и четыре
Telegram-аккаунта либо переиспользовать нужные подключения. Runtime разрешает
ключ и endpoint строго из профиля конкретного агента.

Перед каждым ответом агент получает текущее время (в выбранном часовом поясе и
UTC), время последнего сообщения, недавнюю переписку, сохранённую сводку старой
части диалога и релевантные факты Mem0. Контексты разных пользователей и
агентов не смешиваются.

## Безопасность

- API-ключи, Telegram API hash и MCP environment шифруются перед записью в БД.
- Существующие секреты никогда не возвращаются из API или в формы панели.
- Файлы Telethon session и ключи не коммитятся.
- Опасные Telegram-действия блокируются политиками и требуют разрешения администратора.
- Перед массовыми рассылками учитывайте правила Telegram и применимое законодательство.

## Проверки

```bash
cd backend
pytest
ruff check .

cd ../frontend
npm run build
```