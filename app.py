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

#Creación del índice del contenido que será desde la base de datos.
def create_index_from_content(content_text):
    """
    Crea un índice Annoy dividiendo el contenido en fragmentos (chunking).
    """
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,      # Tamaño máximo de tokens por fragmento
        chunk_overlap=100     # Superposición entre fragmentos
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
    max_tokens=150
)
        db_chain = RetrievalQA.from_chain_type(
            llm=llm,
            chain_type="stuff",
            retriever=vectorstore.as_retriever()
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
