# CINEMA 360 — Flask + SQLite starter

## Run in Termux
```bash
pkg update
pkg install python -y
pip install -r requirements.txt
python app.py
```
Open `http://127.0.0.1:5000`

## First login
- Admin username: `admin`
- Admin password: `change-this-password`

Change the password before hosting publicly. Set a strong `SECRET_KEY` environment variable before deployment.

## Included in this starter
- 360° panorama viewer (Pannellum CDN)
- Admin image upload and absolute position picker
- Add/edit movie and showtimes
- 20-seat layout per show (5 x 4 demo layout)
- UPI QR upload and UTR submission
- Admin payment approval; generates a ticket ID
- User-facing booking status, ticket view, and message/notification

## Important
This is a starter project. Before accepting real payments or exposing it publicly, add production-grade authentication, CSRF protection, rate limiting, HTTPS, privacy controls, and proper payment verification. A UTR entered by a user is not proof of payment; verify it in your UPI/bank app before approval.


## UI update
- The 360° viewer is now edge-to-edge in the full-screen hero; navigation overlays it transparently.
- Booking opens a numbered 20-seat grid (A1–A5, B1–B5, C1–C5, D1–D5) and disables seats with pending/confirmed bookings for that show.
- In Admin > panorama > Pick Seat Positions, choose a seat and click the exact spot in the panorama to capture that direction; save the position. Selecting that seat in the booking grid moves the public panorama to the saved position.
