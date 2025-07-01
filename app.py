import os
import logging

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

#Aquí cargo las variables de entorno desde el archivo .env
load_dotenv()

#Configuraciones de variables globales
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# Inicializar la aplicación Flask
app = Flask(__name__)
CORS(app)  # Permitir solicitudes CORS

# Variables de configuración
db_chain = None

# Prompt personalizado para asesor de ventas
base_prompt = PromptTemplate(
    input_variables=["query"],
    template=(
        "Como asesor de ventas, responde de forma clara y completa pero concisa a la siguiente consulta:\n"
        "'{query}'\n"
        "Usa solo el contenido disponible. Si no hay información suficiente, sugiere llamar al 5580050900.\n"
        "Limita la respuesta a lo esencial, sin exceder lo necesario. No expliques más de lo necesario.\n"
        "Si la pregunta no está relacionada con productos o servicios del sitio, responde:\n"
        "'Lo siento, solo tengo información del sitio.'"
    )
)

#Creación del índice del contenido que será desde la base de datos.
def create_index_from_content(content_text):
    """
    Crea un índice Annoy dividiendo el contenido en fragmentos (chunking).
    """
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=700,      # Tamaño máximo de tokens por fragmento
        chunk_overlap=50     # Superposición entre fragmentos
    )
    documents = text_splitter.create_documents([content_text])  # ← Aquí se parte el contenido

    embeddings = OpenAIEmbeddings(api_key=OPENAI_API_KEY)
    vectorstore = Annoy.from_documents(documents, embeddings)  # ← Se indexan por fragmento

    return vectorstore


@app.route('/setup-db', methods=['POST'])
def setup_content():
    """
    Ruta para configurar el contenido que será usado en el sistema de consulta.

    :return: Mensaje de éxito o error en la configuración del contenido.
    """
    global db_chain

#Recuperación de datos de la solicitud. Estos datos los recibimos en un request en formato jason.

    data = request.json
    content_text = data.get('contenido')



    if not content_text:
        return jsonify({'error': 'El campo contenido es requerido'}), 400

    try:
        # Crear el índice de Annoy con el contenido proporcionado
        vectorstore = create_index_from_content(content_text)

        # Crear el chatbot para consultas usando el modelo de lenguaje
        llm = ChatOpenAI(
    model="gpt-3.5-turbo",
    api_key=OPENAI_API_KEY,
    temperature=0,
    max_tokens=100
)
        db_chain = RetrievalQA.from_chain_type(
            llm=llm,
            chain_type="stuff",
            retriever=vectorstore.as_retriever(search_kwargs={"k": 2})
        )

        return jsonify({'message': 'Contenido configurado con éxito'}), 200

#Manejo de errorres
    except Exception as e:
        logging.error(f"Error en setup_content: {str(e)}")
        return jsonify({'error': f'Error al configurar el contenido: {str(e)}'}), 500

#Permite ejecutar consultas a través de Lan Chain
def execute_langchain_query(query):
    """
    Ejecuta una consulta usando LangChain y el contenido configurado.

    :param query: La consulta del usuario.
    :return: Resultado de la consulta.
    """
    if not db_chain:
        raise ValueError("El contenido no está configurado correctamente")

    result = db_chain.invoke({"query": query})  # Invocar la consulta
    return result.get('result', '').strip()  # Devolver el resultado formateado


@app.route('/')
def index():
    """
    Ruta principal que renderiza la página de inicio.
    """
    return render_template('index.html')


@app.route('/chat', methods=['POST'])
def chat():
    """
    Ruta para manejar las consultas del usuario.

    :return: Respuesta del chatbot con la consulta y su respuesta.
    """
    user_message = request.json.get('message')
    logging.info("Mensaje del usuario: %s", user_message)

    try:
        result = execute_langchain_query(user_message)  # Ejecutar la consulta
        return jsonify({"question": user_message, "response": result.strip()})


    except Exception as e:
        logging.exception("Error inesperado: %s", e)
        return jsonify({"error": str(e)}), 500


# Ejecutar la aplicación en el puerto especificado
if __name__ == '__main__':
    app.run(debug=False, port=5010)
