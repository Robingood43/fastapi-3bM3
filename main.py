# В documents будут егрн, квитанции, устав, фз, свидетельство
#в news будут сметы, собрания, объявления, отчеты, акты
import asyncio
import datetime
import glob
import os
from concurrent.futures import ThreadPoolExecutor
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import lru_cache

import aiosmtplib
from fastapi import FastAPI, Request, Form, UploadFile, status, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.security.utils import get_authorization_scheme_param
from fastapi.staticfiles import StaticFiles
from fitz import fitz
from jose import jwt, JWTError
from passlib.context import CryptContext
from pdfminer.high_level import extract_text
from pdfminer.layout import LAParams
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.templating import Jinja2Templates

from crud import Crud
from db import engine
from models import Ad, Document, ContactForm, MyUploadFile, News

app = FastAPI(docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key="some-random-string")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
session = async_sessionmaker(bind=engine, expire_on_commit=False)
db = Crud()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Определяем токен для аутентификации
SECRET_KEY = str(os.getenv('secret_key'))
ALGORITHM = os.getenv('ALGORITHM')
ACCESS_TOKEN_EXPIRE_MINUTES = 30

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


@app.get("/open/{filename}", response_class=HTMLResponse)
async def open_pdf(filename: str, request: Request):
    if os.path.exists(f"static/temporary/{filename}"):
        return templates.TemplateResponse("pdf_viewer.html",
                                          {"request": request,
                                           "photos": [f"static/temporary/{filename}/{i}"
                                                      for i in sorted(os.listdir(f"static/temporary/{filename}/"))],
                                           'filename': filename})
    else:
        return await create_and_save_pixmaps(request, filename)


async def create_and_save_pixmaps(request, filename):
    loop = asyncio.get_event_loop()
    try:
        pdf_images_path = await loop.run_in_executor(None, save_pixmaps_to_file_lru, f'static/news/{filename}',
                                                     filename)
    except fitz.fitz.FileNotFoundError:
        pdf_images_path = await loop.run_in_executor(None, save_pixmaps_to_file_lru, f'static/documents/{filename}',
                                                     filename)

    return templates.TemplateResponse("pdf_viewer.html",
                                      {"request": request, "photos": sorted(pdf_images_path), 'filename': filename})


@lru_cache(maxsize=128)
def save_pixmaps_to_file_lru(file_path, filename):
    return save_pixmaps_to_file(file_path, filename)


def save_pixmaps_to_file(file_path, filename):
    pdf_images = []
    doc = fitz.open(file_path)  # open document
    os.makedirs(f'static/temporary/{filename}', exist_ok=True)
    for page in doc:  # iterate through the pages
        pix = page.get_pixmap()  # render page to an image
        png_path = f"static/temporary/{filename}/{filename}page-{page.number}.png"
        pix.save(png_path)
        pdf_images.append(png_path)

    return pdf_images


executor = ThreadPoolExecutor()

@lru_cache(maxsize=128)
def extract_text_cached(filename):
    return extract_text(
        filename,
        maxpages=1,
        laparams=LAParams(boxes_flow=None)
        ).splitlines()

async def extract_text_async(filename):
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(executor, extract_text_cached, filename)
    return result

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    messages = request.session.pop('flash', {})
    ads = []

    news = await db.get_all_news(session)
    tasks = [extract_text_async(f'static/news/{n.filename}') for n in news if '.pdf' in n.filename]
    descriptions = await asyncio.gather(*tasks)

    for i, desc in zip(news, descriptions):
        if '.pdf' in i.filename:
            ads.append(Ad(
                title=i.filename,
                date_add=i.date_add,
                description=desc,
                more="Подробнее"
                ))

    return templates.TemplateResponse("index.html", {"request": request, "ads": ads, "messages": messages})

@app.get("/news/{document_name}", response_class=HTMLResponse)
async def download_news(document_name):
    return FileResponse(path="static/news/"+document_name)


@app.get("/news.html", response_class=HTMLResponse)
async def get_news(request: Request):
    all_data = await db.get_all_news(session)
    all_news_db = {i.filename for i in all_data}
    all_news_path = {os.path.basename(i) for i in glob.glob("static/news/*")}
    news_add = all_news_path.difference(all_news_db)
    news_delete = all_news_db.difference(all_news_path)

    if news_add:
        news_docs = [News(size = f"{(os.path.getsize('static/news/'+filename) / 1024):.2f}",
                            filename = filename,
                            date_add = datetime.datetime.now().date())for filename in news_add]
        await db.add_news(session, news_docs)

    if news_delete:
        await db.delete_news(session, list(news_delete))
    return templates.TemplateResponse("news.html", {"request": request, "news": await db.get_all_news(session)})



@app.get("/documents/{document_name}", response_class=HTMLResponse)
async def download_document(document_name):
    return FileResponse(path="static/documents/"+document_name)


@app.get("/documents.html", response_class=HTMLResponse)
async def get_documents(request: Request):
    all_data = await db.get_all_docs(session)
    all_docs_db = {i.filename for i in all_data}
    all_docs_path = {os.path.basename(i) for i in glob.glob("static/documents/*")}
    docs_add = all_docs_path.difference(all_docs_db)
    docs_delete = all_docs_db.difference(all_docs_path)

    if docs_add:

        new_docs = [Document(size = f"{(os.path.getsize('static/documents/'+filename) / 1024):.2f}",
                            filename = filename,
                            date_add = datetime.datetime.now().date())for filename in docs_add]
        await db.add_docs(session, new_docs)

    if docs_delete:
        await db.delete_docs(session, list(docs_delete))

    return templates.TemplateResponse("documents.html", {"request": request, "documents": await db.get_all_docs(session)})


@app.get("/contact.html", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("contact.html", {"request": request})


@app.post("/send_email/")
async def create_upload_file(user_name: str = Form(), user_email: str = Form(),user_phone: str = Form(), MESSAGE: str = Form(), file: UploadFile = Form(...)):
    form = ContactForm(
        user_name=user_name,
        user_email=user_email,
        user_phone=user_phone,
        message=MESSAGE,
        file = await MyUploadFile.from_uploadfile(file)
    )

    msg = MIMEMultipart('mixed')
    # Добавление текстовой части сообщения
    text_part = MIMEText(f"Name: {form.user_name} \nemail: {form.user_email} \ntel_number: {form.user_phone} \nmessage: {form.message}", 'plain')
    msg.attach(text_part)
    if form.file.filename:
        # Добавление вложения
        attachment_part = MIMEApplication(form.file.filetext, Name=form.file.filename)
        attachment_part['Content-Disposition'] = f'attachment; filename="{form.file.filename}"'
        msg.attach(attachment_part)

    # Заголовки сообщения
    msg['From'] = 'sharapov.kirill.site@ya.ru'
    msg['To'] = 'sharapov.kirill.site@ya.ru'
    msg['Subject'] = 'Письмо с сайта'

    # Подключение к серверу SMTP и отправка сообщения
    async with aiosmtplib.SMTP('smtp.yandex.com', 587) as smtp:
        await smtp.login('sharapov.kirill.site@ya.ru', os.getenv('PASSWORD'))
        await smtp.send_message(msg)

    return JSONResponse(status_code=200, content={"message": "Форма успешно отправлена"})


@app.get("/auth.html", response_class=HTMLResponse)
async def read_root(request: Request):
    messages = request.session.pop('flash', {})
    return templates.TemplateResponse("auth.html", {"request": request, 'messages':messages})


# Генерация хэша пароля
def get_password_hash(password):
    return pwd_context.hash(password)


# Проверка хэша пароля
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


# Генерация токена
def create_access_token(data: dict):
    to_encode = data.copy()
    expires_delta = datetime.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    expire = datetime.datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire})

    encoded_jwt = jwt.encode(to_encode, str(SECRET_KEY), algorithm=ALGORITHM)

    return encoded_jwt


# Декодирование токена
def decode_access_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid credentials")


@app.post("/token")
async def login(response: Request, form_data: OAuth2PasswordRequestForm = Depends(), ):

    user = await db.user_check(session, form_data.username)
    if not user:
        response.session["flash"] = {"error": "Неверные данные!"}
        return RedirectResponse(url='/auth.html', status_code=status.HTTP_302_FOUND)
    if not verify_password(form_data.password, user.hashed_password):
        response.session["flash"] = {"error": "Неверные данные!"}
        return RedirectResponse(url='/auth.html', status_code=status.HTTP_302_FOUND)
    response.session["flash"] = {"success": "Успешная авторизация!"}
    access_token = create_access_token(data={"sub": user.username})
    # Установка JWT как cookie
    response.session["access_token"] = f"bearer {access_token}"
    return RedirectResponse(url='/', status_code=status.HTTP_302_FOUND)


def cookie_oauth2_scheme(request: Request):
    token = request.session.get("access_token")
    scheme, authorization = get_authorization_scheme_param(token)
    if not authorization or scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    return authorization


@app.get("/profile.html", response_class=HTMLResponse)
async def get_profile(request: Request, token: str = Depends(cookie_oauth2_scheme)):
    details = decode_access_token(token)
    if details['sub']:
        return templates.TemplateResponse("profile.html", {"request": request, "username": details['sub']})
    else:
        raise HTTPException(status_code=400, detail="Invalid details")



# # Создание пользователя
# @app.post("/users/")
# async def create_user(user: User):
#
#     db_user = await db.user_create(session, user)
#     if db_user:
#         raise HTTPException(status_code=400, detail="Username already registered")
#
#     hashed_password = get_password_hash(user.password)
#     new_user = User(username=user.username, hashed_password=hashed_password)
#     await db.user_add(session, new_user)
#
#     return {"message": "User created successfully", "user_id": new_user.id}
