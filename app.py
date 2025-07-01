import os
import logging
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from dotenv import load_dotenv
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_community.vectorstores import Annoy
from langchain.chains import RetrievalQA
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.prompts import PromptTemplate

load_dotenv()

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

app = Flask(__name__)
CORS(app)

db_chain = None

# Prompt optimizado
base_prompt = PromptTemplate(
    input_variables=["query"],
    template=(
        "Actúa como un asesor de ventas experto. Responde brevemente la consulta: '{query}', "
        "usando sólo la información disponible. Si no puedes ayudar, indica que el cliente debe comunicarse "
        "al 5580050900 (lunes a viernes, 8am a 5:30pm). Máximo 100 palabras."
    )
)

def create_index_from_content(content_text):
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=600,
        chunk_overlap=50
    )
    documents = text_splitter.create_documents([content_text])

    embeddings = OpenAIEmbeddings(api_key=OPENAI_API_KEY)
    vectorstore = Annoy.from_documents(documents, embeddings)

    return vectorstore

@app.route('/setup-db', methods=['POST'])
def setup_content():
    global db_chain
    data = request.json
    content_text = data.get('contenido')

    if not content_text:
        return jsonify({'error': 'El campo contenido es requerido'}), 400

    try:
        vectorstore = create_index_from_content(content_text)

        retriever = vectorstore.as_retriever(search_kwargs={"k": 2})
        llm = ChatOpenAI(
            model="gpt-3.5-turbo",
            api_key=OPENAI_API_KEY,
            temperature=0,
            max_tokens=150
        )

        db_chain = RetrievalQA.from_chain_type(
            llm=llm,
            chain_type="map_reduce",
            retriever=retriever,
            chain_type_kwargs={"prompt": base_prompt}
        )

        return jsonify({'message': 'Contenido configurado con éxito'}), 200

    except Exception as e:
        logging.error(f"Error en setup_content: {str(e)}")
        return jsonify({'error': f'Error al configurar el contenido: {str(e)}'}), 500

def execute_langchain_query(query):
    if not db_chain:
        raise ValueError("El contenido no está configurado correctamente")

    result = db_chain.invoke({"query": query})
    return result.get('result', '').strip()

@app.route('/')
def index():
    return render_template('index.html')

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

if __name__ == '__main__':
    app.run(debug=False, port=5010)





