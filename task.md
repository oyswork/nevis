Nevis Home Assignment
This exercise is designed to simulate a realistic feature you might work on with us.
You may use one of the backend languages (Java/Kotlin, Python).
We are building a WealthTech automation platform for advisors, who need to search across
clients and clients’ documents quickly and intelligently.
Your task is to implement a simplified Search API across clients and documents that
supports following cases:
● Find clients for matches in their emails/name/description. E.g. request “NevisWealth”
should return client with email “john.doe@neviswealth.com”
● Find documents based on similar terms from its content. E.g. searching for "address
proof" should also return documents containing "utility bill".
● Optional: Quick summary of document content.
Requirements
We encourage you to use LLMs to implement solutions as best as possible.
Open API Endpoints and data model
Follow REST conventions and return proper HTTP codes. Document the API (Swagger or
Markdown) and extend model responses when it's needed.

openapi: 3.1.0
info:
title: API
version: 1.0.0
paths:
/clients:
post:
request_body:
required: true
content:
application/json:
schema:
type: object
required: [first_name, last_name, email]

properties:
first_name:
type: string
last_name:
type: string
email:
type: string
format: email
description:
type: string
social_links:
type: array
items:
type: string

responses:
"201":
content:
application/json:
schema:
$ref: "#/components/schemas/Client"

/clients/{id}/documents:
post:
parameters:
- name: id
in: path
required: true
schema:
type: string
request_body:
required: true
content:
application/json:
schema:
type: object
required: [title, content]
properties:
title:
type: string

content:
type: string

responses:
"201":
content:
application/json:
schema:
$ref: "#/components/schemas/Document"

/search:
get:
parameters:
- name: q
in: query
required: true
schema:
type: string
responses:
"200":
content:
application/json:
schema:
type: array
items:
type: object

components:
schemas:
Client:
type: object
properties:
id:
type: string
first_name:
type: string
last_name:
type: string
email:
type: string

format: email
description:
type: string
social_links:
type: array
items:
type: string

Document:
type: object
properties:
id:
type: string
client_id:
type: string
title:
type: string
content:
type: string
created_at:
type: string
format: date-time

Deliverables
● Source code in a repo.
● docker-compose for reproducibility.
● Tests for core logic and edge cases.
● README.md with:
○ Setup instructions
○ Example search queries and responses
● API documentation (Swagger or Markdown).
● If you can deploy it somewhere and provide us credentials to API - will be a plus
