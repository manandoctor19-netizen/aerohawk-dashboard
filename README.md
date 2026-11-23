# Aerohawk developer dashboard

Team-only dashboard for the Aerohawk campus delivery robot: live camera, robot status, GPS map,
all orders with override buttons, the Kitchen tab, and Autonomous / Manual / STOP.

Deploy on Streamlit Community Cloud with main file `dev_app.py`. In the app's **Secrets**:
```
dev_password = "your-team-password"
[firebase]
...the service-account fields...
```
The password is required: there is no built-in default. **Never commit the Firebase key or secrets.toml.**
