# main.py - FastAPI Backend Server
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl
from typing import Optional, List
import sqlite3
import string
import random
import datetime
from urllib.parse import urlparse
import qrcode
import io
import base64
import uvicorn
import os
from contextlib import contextmanager

app = FastAPI(
    title="URL Shortener API",
    description="A powerful URL shortener with analytics",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
BASE_URL = os.getenv('BASE_URL', 'http://localhost:5001')
DATABASE_FILE = 'urlshortener.db'

# Pydantic Models
class URLCreate(BaseModel):
    original_url: str
    custom_code: Optional[str] = None
    expires_in_days: Optional[int] = None

class URLResponse(BaseModel):
    id: int
    original_url: str
    short_code: str
    short_url: str
    created_at: str
    clicks: int
    qr_code: str

class ClickAnalytics(BaseModel):
    short_code: str
    ip_address: str
    user_agent: str
    referrer: Optional[str]
    clicked_at: str

class AnalyticsResponse(BaseModel):
    total_links: int
    total_clicks: int
    today_clicks: int
    click_data: List[dict]

# Database functions
@contextmanager
def get_db_connection():
    """Get database connection with context manager"""
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_database():
    """Initialize the SQLite database with required tables"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # URLs table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_url TEXT NOT NULL,
                short_code TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                clicks INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT 1,
                expires_at TIMESTAMP NULL
            )
        ''')
        
        # Analytics table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                short_code TEXT NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                referrer TEXT,
                clicked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (short_code) REFERENCES urls (short_code)
            )
        ''')
        
        # Create indexes for better performance
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_short_code ON urls (short_code)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_analytics_short_code ON analytics (short_code)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_clicked_at ON analytics (clicked_at)')
        
        conn.commit()

# Utility functions
def generate_short_code(length: int = 6) -> str:
    """Generate a random short code"""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))

def is_valid_url(url: str) -> bool:
    """Validate URL format"""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except:
        return False

def generate_qr_code(url: str) -> str:
    """Generate QR code for URL and return as base64 string"""
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    
    qr_base64 = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{qr_base64}"

def get_client_ip(request: Request) -> str:
    """Extract client IP address from request"""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host

# API Routes

@app.on_event("startup")
async def startup_event():
    """Initialize database on startup"""
    init_database()

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"message": "URL Shortener API is running", "status": "healthy"}

@app.post("/api/shorten", response_model=URLResponse)
async def shorten_url(url_data: URLCreate, request: Request):
    """Shorten a long URL"""
    
    # Validate URL
    if not is_valid_url(url_data.original_url):
        raise HTTPException(status_code=400, detail="Invalid URL format")
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Check if URL already exists
        cursor.execute(
            "SELECT * FROM urls WHERE original_url = ? AND is_active = 1",
            (url_data.original_url,)
        )
        existing_url = cursor.fetchone()
        
        if existing_url:
            # Return existing short URL
            short_url = f"{BASE_URL}/{existing_url['short_code']}"
            qr_code = generate_qr_code(short_url)
            
            return URLResponse(
                id=existing_url['id'],
                original_url=existing_url['original_url'],
                short_code=existing_url['short_code'],
                short_url=short_url,
                created_at=existing_url['created_at'],
                clicks=existing_url['clicks'],
                qr_code=qr_code
            )
        
        # Generate unique short code
        short_code = url_data.custom_code
        if not short_code:
            while True:
                short_code = generate_short_code()
                cursor.execute("SELECT id FROM urls WHERE short_code = ?", (short_code,))
                if not cursor.fetchone():
                    break
        else:
            # Check if custom code already exists
            cursor.execute("SELECT id FROM urls WHERE short_code = ?", (short_code,))
            if cursor.fetchone():
                raise HTTPException(status_code=400, detail="Custom code already exists")
        
        # Calculate expiration date
        expires_at = None
        if url_data.expires_in_days:
            expires_at = datetime.datetime.now() + datetime.timedelta(days=url_data.expires_in_days)
        
        # Insert new URL
        cursor.execute('''
            INSERT INTO urls (original_url, short_code, expires_at)
            VALUES (?, ?, ?)
        ''', (url_data.original_url, short_code, expires_at))
        
        url_id = cursor.lastrowid
        conn.commit()
        
        # Generate response
        short_url = f"{BASE_URL}/{short_code}"
        qr_code = generate_qr_code(short_url)
        
        return URLResponse(
            id=url_id,
            original_url=url_data.original_url,
            short_code=short_code,
            short_url=short_url,
            created_at=datetime.datetime.now().isoformat(),
            clicks=0,
            qr_code=qr_code
        )

@app.get("/{short_code}")
async def redirect_url(short_code: str, request: Request):
    """Redirect to original URL and track analytics"""
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Get URL data
        cursor.execute('''
            SELECT * FROM urls 
            WHERE short_code = ? AND is_active = 1
        ''', (short_code,))
        
        url_data = cursor.fetchone()
        
        if not url_data:
            raise HTTPException(status_code=404, detail="Short URL not found")
        
        # Check if URL has expired
        if url_data['expires_at']:
            expires_at = datetime.datetime.fromisoformat(url_data['expires_at'])
            if datetime.datetime.now() > expires_at:
                raise HTTPException(status_code=410, detail="Short URL has expired")
        
        # Log analytics
        ip_address = get_client_ip(request)
        user_agent = request.headers.get("User-Agent", "")
        referrer = request.headers.get("Referer", "")
        
        cursor.execute('''
            INSERT INTO analytics (short_code, ip_address, user_agent, referrer)
            VALUES (?, ?, ?, ?)
        ''', (short_code, ip_address, user_agent, referrer))
        
        # Update click count
        cursor.execute('''
            UPDATE urls SET clicks = clicks + 1 WHERE short_code = ?
        ''', (short_code,))
        
        conn.commit()
        
        return RedirectResponse(url=url_data['original_url'], status_code=302)

@app.get("/api/analytics", response_model=AnalyticsResponse)
async def get_analytics():
    """Get overall analytics data"""
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Total links
        cursor.execute("SELECT COUNT(*) as count FROM urls WHERE is_active = 1")
        total_links = cursor.fetchone()['count']
        
        # Total clicks
        cursor.execute("SELECT COUNT(*) as count FROM analytics")
        total_clicks = cursor.fetchone()['count']
        
        # Today's clicks
        today = datetime.date.today().isoformat()
        cursor.execute('''
            SELECT COUNT(*) as count FROM analytics 
            WHERE DATE(clicked_at) = ?
        ''', (today,))
        today_clicks = cursor.fetchone()['count']
        
        # Click data for last 7 days
        cursor.execute('''
            SELECT DATE(clicked_at) as date, COUNT(*) as clicks
            FROM analytics
            WHERE clicked_at >= date('now', '-7 days')
            GROUP BY DATE(clicked_at)
            ORDER BY date
        ''')
        
        click_data = [{"date": row['date'], "clicks": row['clicks']} for row in cursor.fetchall()]
        
        return AnalyticsResponse(
            total_links=total_links,
            total_clicks=total_clicks,
            today_clicks=today_clicks,
            click_data=click_data
        )

@app.get("/api/analytics/{short_code}")
async def get_url_analytics(short_code: str):
    """Get analytics for a specific short URL"""
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Get URL info
        cursor.execute("SELECT * FROM urls WHERE short_code = ?", (short_code,))
        url_data = cursor.fetchone()
        
        if not url_data:
            raise HTTPException(status_code=404, detail="Short URL not found")
        
        # Get click analytics
        cursor.execute('''
            SELECT ip_address, user_agent, referrer, clicked_at
            FROM analytics
            WHERE short_code = ?
            ORDER BY clicked_at DESC
        ''', (short_code,))
        
        clicks = [
            {
                "ip_address": row['ip_address'],
                "user_agent": row['user_agent'],
                "referrer": row['referrer'],
                "clicked_at": row['clicked_at']
            }
            for row in cursor.fetchall()
        ]
        
        return {
            "url_info": {
                "original_url": url_data['original_url'],
                "short_code": url_data['short_code'],
                "created_at": url_data['created_at'],
                "total_clicks": url_data['clicks']
            },
            "clicks": clicks
        }

@app.get("/api/links")
async def get_all_links():
    """Get all shortened links"""
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, original_url, short_code, created_at, clicks
            FROM urls
            WHERE is_active = 1
            ORDER BY created_at DESC
        ''')
        
        links = []
        for row in cursor.fetchall():
            short_url = f"{BASE_URL}/{row['short_code']}"
            links.append({
                "id": row['id'],
                "original_url": row['original_url'],
                "short_code": row['short_code'],
                "short_url": short_url,
                "created_at": row['created_at'],
                "clicks": row['clicks']
            })
        
        return {"links": links}

@app.delete("/api/links/{short_code}")
async def delete_link(short_code: str):
    """Deactivate a shortened link"""
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE urls SET is_active = 0 
            WHERE short_code = ?
        ''', (short_code,))
        
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Short URL not found")
        
        conn.commit()
        
        return {"message": "Link deactivated successfully"}

# Error handlers
@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=404,
        content={"error": "Endpoint not found"}
    )

@app.exception_handler(500)
async def internal_error_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"}
    )

if __name__ == "__main__":
    print("🚀 Starting URL Shortener API...")
    print("📡 Server will be available at: http://localhost:5001")
    print("📚 API Documentation: http://localhost:5001/docs")
    print("🔍 Health Check: http://localhost:5001")
    print("\n" + "="*50)
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=5001,
        reload=True,
        log_level="info"
    )