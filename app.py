import os
import logging
import mysql.connector  # ← Agregado

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from dotenv import load_dotenv
from langchain_openai import OpenAI
from langchain.prompts import PromptTemplate
from langchain_community.document_loaders import TextLoader
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_community.vectorstores import Annoy
from langchain.chains import RetrievalQA
from langchain.text_splitter import RecursiveCharacterTextSplitter  # ← Agrego esto para implementar el chunking

# Cargar variables de entorno
load_dotenv()

# Configuración de API y base de datos
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

# Inicializar Flask
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins=["https://masterconnect.com.mx"])

# Estado global del sistema
db_chain = None

# Prompt personalizado para asesor de ventas
base_prompt = PromptTemplate(
    input_variables=["query"],
    template=(
        "Actúa como un asesor de ventas experto y amigable. "
        "Responde brevemente y de forma concisa a la consulta: '{query}', "
        "usando únicamente la información proporcionada en el contenido. "
        "No agregues explicaciones adicionales ni detalles innecesarios. "
        "Mantén la respuesta clara, corta y orientada a ayudar al cliente a tomar una decisión de compra. "
        "En caso de no contar con el artículo solicitado, recomienda otros productos similares. "
        "Si no puedes responder con la información disponible, invita cordialmente a comunicarse con un asesor por Teléfono o WhatsApp al 5580050900, de Lunes a Viernes, de 8:00 am a 5:30 pm. "
        "Limita tu respuesta a un máximo de 150 palabras, no cortes las frases, procura en tu límite escribir la respuesta comleta. "
    )
)

# Guardar conversación en base de datos
def guardar_conversacion(session_id, question, response):
    try:
        conn = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        cursor = conn.cursor()
        query = "INSERT INTO chat_logs (session_id, user_question, bot_response) VALUES (%s, %s, %s)"
        cursor.execute(query, (session_id, question, response))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logging.error(f"Error al guardar en BD: {str(e)}")

# Crear índice vectorial del contenido usando chunking
def create_index_from_content(content_text):
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=100
    )
    documents = text_splitter.create_documents([content_text])
    embeddings = OpenAIEmbeddings(api_key=OPENAI_API_KEY)
    vectorstore = Annoy.from_documents(documents, embeddings)
    return vectorstore

# Ruta para configurar el contenido
@app.route('/setup-db', methods=['POST'])
def setup_content():
    global db_chain
    data = request.json
    content_text = data.get('contenido')

    if not content_text:
        return jsonify({'error': 'El campo contenido es requerido'}), 400

    try:
        vectorstore = create_index_from_content(content_text)
        llm = ChatOpenAI(
            model="gpt-3.5-turbo",
            api_key=OPENAI_API_KEY,
            temperature=0,
            max_tokens=150
        )
        db_chain = RetrievalQA.from_chain_type(
            llm=llm,
            chain_type="stuff",
            retriever=vectorstore.as_retriever()
        )
        return jsonify({'message': 'Contenido configurado con éxito'}), 200
    except Exception as e:
        logging.error(f"Error en setup_content: {str(e)}")
        return jsonify({'error': f'Error al configurar el contenido: {str(e)}'}), 500

# Ejecutar consulta a LangChain
def execute_langchain_query(query):
    if not db_chain:
        raise ValueError("El contenido no está configurado correctamente")
    result = db_chain.invoke({"query": query})
    return result.get('result', '').strip()

# Página de inicio
@app.route('/')
def index():
    return render_template('index.html')

# Endpoint de chat con almacenamiento de conversación
@app.route('/chat', methods=['POST'])
def chat():
    user_message = request.json.get('message')
    session_id = request.remote_addr  # Puedes usar también cookies o tokens únicos
    logging.info("Mensaje del usuario: %s", user_message)

    try:
        result = execute_langchain_query(user_message)
        guardar_conversacion(session_id, user_message, result)
        return jsonify({"question": user_message, "response": result.strip()})
    except Exception as e:
        logging.exception("Error inesperado: %s", e)
        return jsonify({"error": str(e)}), 500

# Ejecutar la aplicación
if __name__ == '__main__':
    app.run(debug=False, port=5010)
