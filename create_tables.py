from database import engine, Base

from models.user import User
from models.product import Product
from models.order import Order
from models.deposit import Deposit
from models.ticket import Ticket

print("Creating tables...")

Base.metadata.create_all(bind=engine)

print("✅ Tables Created")