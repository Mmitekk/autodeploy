ШАБЛОННЫЙ КОНТЕКСТ ДЛЯ НЕЙРОНКИ
================================
Копируй это в начало каждого нового чата с нейронкой:

---

Работаешь с проектом kuppersbusch.tech (Next.js 16 standalone + Prisma + SQLite).
У тебя есть доступ ТОЛЬКО к Git-репозиторию — нет доступа к серверу.
Каждый git push в main автоматически деплоится на продакшен.
Если билд падает — сайт откатывается на прошлую рабочую версию.
При деплое автоматически запускается `prisma db seed` — данные обновляются.

СТРОГИЕ ПРАВИЛА (читай .cursorrules):
1. НЕ трогай: .env, ecosystem.config.js, next.config.ts, package.json скрипты
2. НЕ меняй: PORT (3001), HOSTNAME, HOST, DATABASE_URL
3. Все SQL-запросы в generateMetadata/layout — ОБЯЗАТЕЛЬНО в try/catch
4. НЕ коммить .env, *.db, .next, public/uploads/
5. Если добавляешь зависимость — запусти npm install, обнови package-lock.json
6. НЕ добавляй скрипты, которые трогают .env

ДАННЫЕ И ИЗОБРАЖЕНИЯ:
7. ВСЕ стоковые изображения → /public/seed-images/ (git-tracked)
8. Пути в БД и seed.ts → ТОЛЬКО /seed-images/... (НЕ /uploads/!)
9. /public/uploads/ — ТОЛЬКО для пользовательских загрузок (в .gitignore)
10. Чтобы добавить/изменить данные (города, товары, картинки):
    - Редактируй prisma/seed.ts (используй upsert)
    - Добавляй картинки в /public/seed-images/
    - Пушь в main → при деплое prisma db seed запустится автоматически
11. НЕ создавай кнопки «Загрузить стоковые изображения» — seed.ts это делает
12. НЕ создавай auto-seed — НЕ вызывай seed при каждом запросе страницы
13. НЕ коммить public/uploads/ — это серверная папка

КАК ДОБАВИТЬ НОВЫЕ ДАННЫЕ:
- Новый город → добавь в массив cities в prisma/seed.ts
- Новый товар → добавь в массив products в prisma/seed.ts, картинку в /public/seed-images/products/
- Новая категория → добавь в массив categories, картинку в /public/seed-images/categories/
- Новая новость → добавь в массив news, картинку в /public/seed-images/news/
- Запушь → при деплое seed обновит БД

ЗАДАЧА: [опиши что нужно сделать]

---
