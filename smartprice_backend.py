# SmartPrice Monitor - Backend API
# File: main.py

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer
from sqlalchemy.orm import Session
from typing import List, Optional
import asyncio
from datetime import datetime, timedelta
import redis
import json

from models import Product, PriceHistory, Alert, User
from database import get_db, engine
from schemas import (
    ProductCreate, ProductResponse, PriceHistoryResponse,
    AlertCreate, AlertResponse, UserCreate
)
from scraper import PriceScraper
from notifications import NotificationService
from auth import get_current_user, create_access_token

# Initialize FastAPI app
app = FastAPI(
    title="SmartPrice Monitor API",
    description="Intelligent price monitoring system for e-commerce",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Redis client
redis_client = redis.Redis(host='localhost', port=6379, db=0)

# Security
security = HTTPBearer()

# Background services
scraper = PriceScraper()
notification_service = NotificationService()

@app.on_event("startup")
async def startup_event():
    """Initialize background tasks and services"""
    asyncio.create_task(background_price_monitor())

async def background_price_monitor():
    """Background task to monitor prices every hour"""
    while True:
        try:
            await scraper.monitor_all_products()
            await asyncio.sleep(3600)  # Run every hour
        except Exception as e:
            print(f"Error in background monitor: {e}")
            await asyncio.sleep(300)  # Retry in 5 minutes

# Authentication endpoints
@app.post("/auth/register")
async def register_user(user: UserCreate, db: Session = Depends(get_db)):
    """Register new user"""
    db_user = User(
        email=user.email,
        username=user.username,
        hashed_password=user.password  # In production, hash this!
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    
    access_token = create_access_token(data={"sub": db_user.email})
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/auth/login")
async def login(credentials: dict, db: Session = Depends(get_db)):
    """Login user"""
    user = db.query(User).filter(User.email == credentials["email"]).first()
    if not user or user.hashed_password != credentials["password"]:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    access_token = create_access_token(data={"sub": user.email})
    return {"access_token": access_token, "token_type": "bearer"}

# Product endpoints
@app.post("/products/", response_model=ProductResponse)
async def create_product(
    product: ProductCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Add new product to monitor"""
    db_product = Product(
        name=product.name,
        url=product.url,
        target_price=product.target_price,
        user_id=current_user.id,
        is_active=True
    )
    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    
    # Start monitoring immediately
    background_tasks.add_task(scraper.scrape_product, db_product.id)
    
    return db_product

@app.get("/products/", response_model=List[ProductResponse])
async def get_products(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get user's monitored products"""
    products = db.query(Product).filter(
        Product.user_id == current_user.id
    ).offset(skip).limit(limit).all()
    return products

@app.get("/products/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get specific product details"""
    product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == current_user.id
    ).first()
    
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    return product

@app.put("/products/{product_id}")
async def update_product(
    product_id: int,
    product_update: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update product settings"""
    product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == current_user.id
    ).first()
    
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    for key, value in product_update.items():
        if hasattr(product, key):
            setattr(product, key, value)
    
    db.commit()
    return {"message": "Product updated successfully"}

@app.delete("/products/{product_id}")
async def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete monitored product"""
    product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == current_user.id
    ).first()
    
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    db.delete(product)
    db.commit()
    return {"message": "Product deleted successfully"}

# Price history endpoints
@app.get("/products/{product_id}/history", response_model=List[PriceHistoryResponse])
async def get_price_history(
    product_id: int,
    days: int = 30,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get price history for a product"""
    # Verify product ownership
    product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == current_user.id
    ).first()
    
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    since_date = datetime.now() - timedelta(days=days)
    
    history = db.query(PriceHistory).filter(
        PriceHistory.product_id == product_id,
        PriceHistory.scraped_at >= since_date
    ).order_by(PriceHistory.scraped_at.desc()).all()
    
    return history

# Analytics endpoints
@app.get("/analytics/dashboard")
async def get_dashboard_data(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get dashboard analytics data"""
    # Total products
    total_products = db.query(Product).filter(
        Product.user_id == current_user.id
    ).count()
    
    # Active monitors
    active_monitors = db.query(Product).filter(
        Product.user_id == current_user.id,
        Product.is_active == True
    ).count()
    
    # Price alerts in last 24h
    since_yesterday = datetime.now() - timedelta(days=1)
    recent_alerts = db.query(Alert).join(Product).filter(
        Product.user_id == current_user.id,
        Alert.created_at >= since_yesterday
    ).count()
    
    # Average savings
    # This would be calculated based on target vs current prices
    
    return {
        "total_products": total_products,
        "active_monitors": active_monitors,
        "recent_alerts": recent_alerts,
        "total_savings": 0.0  # Calculate based on your logic
    }

# Alerts endpoints
@app.get("/alerts/", response_model=List[AlertResponse])
async def get_alerts(
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get user's price alerts"""
    alerts = db.query(Alert).join(Product).filter(
        Product.user_id == current_user.id
    ).order_by(Alert.created_at.desc()).offset(skip).limit(limit).all()
    
    return alerts

# Manual scraping endpoint
@app.post("/products/{product_id}/scrape")
async def manual_scrape(
    product_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Manually trigger price scraping for a product"""
    product = db.query(Product).filter(
        Product.id == product_id,
        Product.user_id == current_user.id
    ).first()
    
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    background_tasks.add_task(scraper.scrape_product, product_id)
    return {"message": "Scraping started"}

# Health check
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.now()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

# File: models.py
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    username = Column(String(100), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    products = relationship("Product", back_populates="user")

class Product(Base):
    __tablename__ = "products"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    url = Column(Text, nullable=False)
    current_price = Column(Float)
    target_price = Column(Float, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_checked = Column(DateTime)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    # Relationships
    user = relationship("User", back_populates="products")
    price_history = relationship("PriceHistory", back_populates="product")
    alerts = relationship("Alert", back_populates="product")

class PriceHistory(Base):
    __tablename__ = "price_history"
    
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    price = Column(Float, nullable=False)
    scraped_at = Column(DateTime, default=datetime.utcnow)
    source = Column(String(100))  # Which scraper was used
    
    # Relationships
    product = relationship("Product", back_populates="price_history")

class Alert(Base):
    __tablename__ = "alerts"
    
    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    alert_type = Column(String(50), nullable=False)  # price_drop, target_reached, etc.
    message = Column(Text, nullable=False)
    is_sent = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    product = relationship("Product", back_populates="alerts")

# File: schemas.py
from pydantic import BaseModel, HttpUrl
from typing import Optional, List
from datetime import datetime

class UserCreate(BaseModel):
    email: str
    username: str
    password: str

class UserResponse(BaseModel):
    id: int
    email: str
    username: str
    is_active: bool
    created_at: datetime
    
    class Config:
        from_attributes = True

class ProductCreate(BaseModel):
    name: str
    url: HttpUrl
    target_price: float

class ProductResponse(BaseModel):
    id: int
    name: str
    url: str
    current_price: Optional[float]
    target_price: float
    is_active: bool
    created_at: datetime
    last_checked: Optional[datetime]
    
    class Config:
        from_attributes = True

class PriceHistoryResponse(BaseModel):
    id: int
    price: float
    scraped_at: datetime
    source: Optional[str]
    
    class Config:
        from_attributes = True

class AlertCreate(BaseModel):
    product_id: int
    alert_type: str
    message: str

class AlertResponse(BaseModel):
    id: int
    alert_type: str
    message: str
    is_sent: bool
    created_at: datetime
    
    class Config:
        from_attributes = True

# File: scraper.py
import aiohttp
import asyncio
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session
from database import SessionLocal
from models import Product, PriceHistory, Alert
import re
from typing import Optional
import random
from datetime import datetime

class PriceScraper:
    def __init__(self):
        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'
        ]
        
    async def get_random_headers(self):
        return {
            'User-Agent': random.choice(self.user_agents),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
    
    async def scrape_product(self, product_id: int):
        """Scrape price for a specific product"""
        db = SessionLocal()
        try:
            product = db.query(Product).filter(Product.id == product_id).first()
            if not product or not product.is_active:
                return
            
            price = await self.extract_price(product.url)
            if price:
                # Save to price history
                price_history = PriceHistory(
                    product_id=product.id,
                    price=price,
                    source="web_scraper"
                )
                db.add(price_history)
                
                # Update current price
                old_price = product.current_price
                product.current_price = price
                product.last_checked = datetime.utcnow()
                
                # Check for alerts
                if price <= product.target_price:
                    alert = Alert(
                        product_id=product.id,
                        alert_type="target_reached",
                        message=f"Target price reached! {product.name} is now ${price}"
                    )
                    db.add(alert)
                elif old_price and price < old_price * 0.9:  # 10% drop
                    alert = Alert(
                        product_id=product.id,
                        alert_type="price_drop",
                        message=f"Significant price drop! {product.name} dropped to ${price}"
                    )
                    db.add(alert)
                
                db.commit()
                
        finally:
            db.close()
    
    async def extract_price(self, url: str) -> Optional[float]:
        """Extract price from product page"""
        try:
            async with aiohttp.ClientSession() as session:
                headers = await self.get_random_headers()
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        html = await response.text()
                        soup = BeautifulSoup(html, 'html.parser')
                        
                        # Common price selectors for different sites
                        price_selectors = [
                            '.price',
                            '.current-price',
                            '.sale-price',
                            '[data-testid="price"]',
                            '.price-now',
                            '.price-current',
                        ]
                        
                        for selector in price_selectors:
                            price_element = soup.select_one(selector)
                            if price_element:
                                price_text = price_element.get_text().strip()
                                price = self.parse_price(price_text)
                                if price:
                                    return price
                        
        except Exception as e:
            print(f"Error scraping {url}: {e}")
        
        return None
    
    def parse_price(self, price_text: str) -> Optional[float]:
        """Parse price from text"""
        # Remove currency symbols and extract numbers
        price_match = re.search(r'[\d,]+\.?\d*', price_text.replace(',', ''))
        if price_match:
            try:
                return float(price_match.group())
            except ValueError:
                pass
        return None
    
    async def monitor_all_products(self):
        """Monitor all active products"""
        db = SessionLocal()
        try:
            products = db.query(Product).filter(Product.is_active == True).all()
            tasks = [self.scrape_product(product.id) for product in products]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            db.close()

# File: database.py
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os

# Database URL - use environment variable in production
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/smartprice")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# File: auth.py
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from database import get_db
from models import User

# Security configuration
SECRET_KEY = "your-secret-key-here"  # Use environment variable in production
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        raise credentials_exception
    return user

# File: notifications.py
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import aiohttp
import json

class NotificationService:
    def __init__(self):
        self.smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.email = os.getenv("EMAIL_USER")
        self.password = os.getenv("EMAIL_PASSWORD")
        self.slack_webhook = os.getenv("SLACK_WEBHOOK_URL")
    
    async def send_email(self, to_email: str, subject: str, message: str):
        """Send email notification"""
        try:
            msg = MIMEMultipart()
            msg['From'] = self.email
            msg['To'] = to_email
            msg['Subject'] = subject
            
            msg.attach(MIMEText(message, 'plain'))
            
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()
            server.login(self.email, self.password)
            text = msg.as_string()
            server.sendmail(self.email, to_email, text)
            server.quit()
        except Exception as e:
            print(f"Failed to send email: {e}")
    
    async def send_slack(self, message: str):
        """Send Slack notification"""
        if not self.slack_webhook:
            return
        
        try:
            async with aiohttp.ClientSession() as session:
                payload = {"text": message}
                async with session.post(self.slack_webhook, json=payload) as response:
                    if response.status != 200:
                        print(f"Failed to send Slack notification: {response.status}")
        except Exception as e:
            print(f"Failed to send Slack notification: {e}")

# File: requirements.txt
fastapi==0.104.1
uvicorn[standard]==0.24.0
sqlalchemy==2.0.23
psycopg2-binary==2.9.9
alembic==1.12.1
pydantic==2.5.0
python-jose[cryptography]==3.3.0
python-multipart==0.0.6
passlib[bcrypt]==1.7.4
redis==5.0.1
celery==5.3.4
aiohttp==3.9.0
beautifulsoup4==4.12.2
requests==2.31.0
pandas==2.1.3
numpy==1.25.2
scikit-learn==1.3.2
plotly==5.17.0
pytest==7.4.3
pytest-asyncio==0.21.1
python-decouple==3.8