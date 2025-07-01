import os
import logging

from langchain.chains.llm import LLMChain
from langchain.chains.combine_documents import StuffDocumentsChain
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from dotenv import load_dotenv
from langchain_openai import OpenAI
from langchain.prompts import PromptTemplate
from langchain_community.document_loaders import TextLoader
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_community.vectorstores import Annoy
from langchain.chains import RetrievalQA
from langchain.text_splitter import RecursiveCharacterTextSplitter

# Cargar variables de entorno desde .env
load_dotenv()

# Configuraciones globales
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# Inicializar la app Flask
app = Flask(__name__)
CORS(app)  # Permitir CORS

# Variable global del sistema de consulta
db_chain = None

# Prompt optimizado para ventas
base_prompt = PromptTemplate(
    input_variables=["query", "context"],
    template=(
        "Responde como asesor de ventas. "
        "Consulta: {query}\n"
        "Información: {context}\n"
        "Responde de forma clara y breve, solo con los datos disponibles. "
        "No expliques ni agregues más. "
        "Si no hay datos, sugiere llamar al 5580050900. "
        "Máximo 80 palabras. "
        "Si la pregunta no es sobre productos o servicios, responde: 'Lo siento, sólo tengo información del sitio.'"
    )
)

# Función para crear el índice de vectores Annoy
def create_index_from_content(content_text):
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=100
    )
    documents = text_splitter.create_documents([content_text])
    embeddings = OpenAIEmbeddings(api_key=OPENAI_API_KEY)
    vectorstore = Annoy.from_documents(documents, embeddings)
    return vectorstore

# Ruta para configurar el contenido (setup-db)
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

        retriever = vectorstore.as_retriever(search_kwargs={"k": 3})

        qa_chain = LLMChain(llm=llm, prompt=base_prompt)

        stuff_chain = StuffDocumentsChain(
            llm_chain=qa_chain,
            document_variable_name="context"
        )

        db_chain = RetrievalQA(
            combine_documents_chain=stuff_chain,
            retriever=retriever
        )

        return jsonify({'message': 'Contenido configurado con éxito'}), 200

    except Exception as e:
        logging.error(f"Error en setup_content: {str(e)}")
        return jsonify({'error': f'Error al configurar el contenido: {str(e)}'}), 500

# Función para ejecutar una consulta
def execute_langchain_query(query):
    if not db_chain:
        raise ValueError("El contenido no está configurado correctamente")

    result = db_chain.invoke({"query": query})
    return result.get('result', '').strip()

# Ruta principal (landing)
@app.route('/')
def index():
    return render_template('index.html')

# Ruta para el endpoint /chat
@app.route('/chat', methods=['POST'])
def chat():
    user_message = request.json.get('message')
    logging.info("Mensaje del usuario: %s", user_message)

    try:
        result = execute_langchain_query(user_message)
        return jsonify({"question": user_message, "response": result.strip()})
    except Exception as e:
        logging.exception("Error inesperado: %s", e)
        return jsonify({"error": str(e)}), 500

# Ejecutar en modo producción
if __name__ == '__main__':
    app.run(debug=False, port=5010)

