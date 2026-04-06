# Car Scraper Project

A Flask web application for scraping car listings from multiple sources and generating comprehensive reports.

## Features

- **Multi-source scraping**: DriveArabia, YallaMotor, OpenSooq
- **Damage detection**: AI-powered image analysis
- **Insurance lookup**: E-insurance claim verification
- **Google image search**: Chassis number image retrieval
- **PDF report generation**: Comprehensive vehicle reports
- **Email functionality**: Automated report delivery
- **Excel export**: Data export capabilities

## Quick Setup

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Install wkhtmltopdf** (for PDF generation):
   - Download from: https://wkhtmltopdf.org/downloads.html
   - Install to default location: `C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe`

3. **Configure environment** (optional):
   - Copy `env_example.txt` to `.env`
   - Modify values as needed
   - If no `.env` file exists, hardcoded defaults will be used

4. **Run the application**:
   ```bash
   python app.py
   ```

5. **Access the application**:
   - Open browser to: `http://localhost:5000`
   - Login with admin credentials

## Configuration

### Environment Variables (Optional)

Create a `.env` file in the project root with any of these variables:

```env
# Email Configuration
SENDER_EMAIL=your-email@gmail.com
SENDER_PASSWORD=your-app-password
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465

# Database Configuration
DB_SERVER=your-server-ip
DB_NAME=ICP
DB_USER=ICP
DB_PASSWORD=your-db-password
DB_DRIVER={ODBC Driver 17 for SQL Server}

# PDF Generation
WKHTMLTOPDF=C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe
```

### Default Values

If no `.env` file is present, the application uses these hardcoded defaults:
- Email: `viditkohli86@gmail.com` with Gmail SMTP
- Database: `208.91.198.196` with SQL Server
- PDF: `C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe`

## Usage

1. **Login**: Access the admin login page
2. **Search**: Enter vehicle criteria and select data sources
3. **Review**: View scraped results, damage detection, and insurance data
4. **Export**: Generate PDF reports or Excel exports
5. **Email**: Send reports directly to users

## Project Structure

```
car_scraper/
├── app.py                 # Main Flask application
├── requirements.txt       # Python dependencies
├── env_example.txt       # Environment variables template
├── scrapers/             # Scraping modules
│   ├── drivearabia.py
│   ├── yallamotor.py
│   ├── opensooq.py
│   ├── insurance_lookup.py
│   ├── google_image.py
│   ├── report_emailer.py
│   └── scraper_utils.py
├── templates/            # HTML templates
├── static/              # Static files and exports
└── logs/               # Application logs
```

## Troubleshooting

- **Chrome driver issues**: Ensure `chromedriver.exe` is in the project root
- **PDF generation fails**: Verify wkhtmltopdf installation path
- **Email sending fails**: Check SMTP credentials and Gmail app password
- **Database connection fails**: Verify SQL Server connection details

## Dependencies

- Flask: Web framework
- Selenium: Web scraping
- Pandas: Data processing
- OpenPyXL: Excel export
- PDFKit: PDF generation
- Pillow: Image processing
- Requests: HTTP requests
- Undetected ChromeDriver: Anti-detection browsing
- PyODBC: Database connectivity
- Python-dotenv: Environment variable loading
