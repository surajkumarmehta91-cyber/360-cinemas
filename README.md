# CINEMA 360 — Easy-to-edit project

This folder contains the same Cinema 360 Flask + SQLite website, arranged so the code is easier to read and edit. The application logic and database schema have not intentionally been redesigned; the HTML templates and CSS have been formatted for readability, and navigation comments were added to `app.py`.

## Folder map

- `app.py` — Flask routes, login, bookings, admin actions, database setup, and seat-position API.
- `templates/` — the HTML pages shown to visitors and admins.
- `templates/index.html` — public homepage, panorama viewer, movie cards, and booking UI.
- `templates/admin.html` — admin dashboard and panorama seat-position picker.
- `templates/admin_movies.html` — movie and showtime management page.
- `templates/admin_payments.html` — payment approval page.
- `templates/admin_bookings.html` — booking management page.
- `templates/base.html` — shared page layout and navigation.
- `static/style.css` — all website styling, colours, spacing, layout, and mobile rules.
- `static/uploads/` — uploaded images are stored here at runtime.
- `requirements.txt` — Python packages required by the project.
- `cinema360.db` — SQLite database created when the app starts; it may not be included in this ZIP.

## Run on a laptop

1. Install Python 3.10 or newer.
2. Open a terminal in the `cinema360_work` folder.
3. Create and activate a virtual environment if desired.
4. Install dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

5. Start the website:

   ```bash
   python app.py
   ```

6. Open `http://127.0.0.1:5000` in your browser.

## First admin login

The starter README previously specified username `admin` and password `change-this-password`. The actual login may depend on the current database/settings, so if those credentials do not work, do not delete or reset the database just to test them. Change any starter password and set a strong `SECRET_KEY` before public deployment.

## Safe editing tips

- Change colours, fonts, spacing, and responsive layout in `static/style.css`. Search for a selector such as `.hero`, `.movie-card`, `.admin-card`, or `@media`.
- Change page text and page structure in the matching file under `templates/`. Keep Flask/Jinja expressions such as `{{ url_for(...) }}`, `{{ ... }}`, `{% if ... %}`, and `{% for ... %}` intact.
- Change server behaviour in `app.py`. The comments divide it into route groups.
- Make a backup before editing. Do not delete `cinema360.db` if you want to keep saved movies, users, bookings, or seat positions.
- Test changes locally before uploading to Render.

## Important

This ZIP is a readability pass, not a security audit. Before accepting real payments or exposing the site publicly, use HTTPS, a strong secret key, CSRF protection, rate limiting, and verified payment processing. A UTR alone is not proof of payment.
