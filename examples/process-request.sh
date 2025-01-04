  # Change "file= ..." if needed
  curl  \
    -H "AA-VERSION: 4.0.3" \
    -H "EX-APP-ID: workflow_ocr_backend" \
    -H "EX-APP-VERSION: 1.0.0" \
    -H "AUTHORIZATION-APP-API: dGVzdDpzZWNyZXQ=" \
    -X POST \
    -F "file=@example-pdf-2.pdf" \
    -F "ocrmypdf_parameters=--image-dpi 300 --skip-big 12.5 --language eng+deu --force-ocr" \
    http://localhost:5000/process_ocr