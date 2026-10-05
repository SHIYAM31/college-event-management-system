pip install -r requirements.txt
flask --app app init-db
flask --app app run

Admin     : admin@college.edu     / admin123
Organizer : organizer@college.edu / org123
Students  : sign up at /signup

Email (optional): set MAIL_SERVER, MAIL_PORT, MAIL_USERNAME, MAIL_PASSWORD, MAIL_FROM
Daily reminders : flask --app app send-reminders
Tests           : python -m unittest tests.test_flow
