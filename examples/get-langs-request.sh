  curl  \
    -H "AA-VERSION: 4.0.3" \
    -H "EX-APP-ID: workflow_ocr_backend" \
    -H "EX-APP-VERSION: 1.0.0" \
    -H "AUTHORIZATION-APP-API: dGVzdDpzZWNyZXQ=" \
    -X GET \
    http://localhost:5000/installed_languages