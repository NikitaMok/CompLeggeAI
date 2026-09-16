![CompLeggeAI](docs/screenshots/banner.jpg)

![Build status](https://img.shields.io/github/actions/workflow/status/NikitaMok/CompLeggeAI/ci.yml?branch=main&style=flat-square&label=CI&labelColor=0B0C10&color=C5A059) ![Python 3.11 and 3.13](https://img.shields.io/badge/python-3.11%20%7C%203.13-C5A059?style=flat-square&labelColor=0B0C10&logo=python&logoColor=EBDAB0) ![Docker Compose](https://img.shields.io/badge/docker-compose-C5A059?style=flat-square&labelColor=0B0C10&logo=docker&logoColor=EBDAB0) ![Ollama](https://img.shields.io/badge/Ollama-llama3.3%3A70b-C5A059?style=flat-square&labelColor=0B0C10&logo=ollama&logoColor=EBDAB0)

![FastAPI](https://img.shields.io/badge/FastAPI-0.115-C5A059?style=flat-square&labelColor=0B0C10&logo=fastapi&logoColor=EBDAB0) ![Uvicorn](https://img.shields.io/badge/Uvicorn-0.34-C5A059?style=flat-square&labelColor=0B0C10&logo=gunicorn&logoColor=EBDAB0) ![Pydantic](https://img.shields.io/badge/Pydantic-2.10-C5A059?style=flat-square&labelColor=0B0C10&logo=pydantic&logoColor=EBDAB0) ![Elasticsearch](https://img.shields.io/badge/Elasticsearch-yente-C5A059?style=flat-square&labelColor=0B0C10&logo=elasticsearch&logoColor=EBDAB0)


[Русская версия](README.md)

CompLeggeAI is intended to perform a multi-layer verification of a foreign trade transaction, comprising the regulatory analysis of the contract, on-chain scoring of the settlement address and verification of the legal status of the parties, followed by the generation of a structured report.

## Limitations of use

CompLeggeAI is a reference tool, not legal advice and/or a legal opinion.

Further details are available here: [docs/LEGAL_DISCLAIMER_EN.md](docs/LEGAL_DISCLAIMER_EN.md).

## Local deployment

```bash
git clone https://github.com/NikitaMok/CompLeggeAI.git
cd CompLeggeAI
cp .env.example .env          
ollama pull llama3.3:70b
docker compose up --build
```

The page opens at `http://127.0.0.1:8000`.

Next, the keys of the verification services have to be entered in `.env`.


| What is checked                    | Variable                                                         |
| ---------------------------------- | ---------------------------------------------------------------- |
| Russian legal entity: EGRUL status | `DADATA_API_KEY`                                                 |
| Russian legal entity: risk markers | `KONTUR_FOCUS_KEY`                                               |
| Crypto address scoring             | `AMLBOT_API_KEY`, `MISTTRACK_API_KEY` or `CHAINALYSIS_API_KEY`   |
| Foreign company                    | `OPENCORPORATES_API_TOKEN`                                       |
| Sanctions and PEP                  | no key required                                                  |




## What the report contains


| First block - The contract:                                                                                                                                                                                                                                                                                                                                                                                                |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 32 rules built on Federal Law No. 282-FZ, Federal Law No. 283-FZ, 115-FZ, 173-FZ and Bank of Russia Instruction No. 181-I: qualification of the transaction, identification of the asset and the network, the identifier address held with the digital depositary, fixing of the rate and of the moment of performance, party details to the extent required by the travel rule, the thresholds of RUB 10m / 60k / 3m, signs of payment splitting. For each breach — the provision cited down to article, part and clause, the consequence and ready-made remedial wording. |



| Second block - The settlement address:                                                                                                                    |
| --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Age of the address, turnover, balance and last activity from public nodes, the risk level and the categories of exposure — from commercial scoring on the client's key. |



| Third block - The parties:                                                                                                                                                                                 |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The legal status of the Russian legal entity arrives as a field from the EGRUL, the risk markers — from Kontur.Focus, the foreign party is searched for in the LEI registry and OpenCorporates, and both parties undergo sanctions and PEP screening. |




## Recommendations on the choice of the local model


| The reference configuration is `llama3.3:70b` in a quantisation no coarser than Q4.                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Such a choice of model is justified by the fact that a crypto foreign trade contract runs to dozens of pages, and a clause that changes the verdict may lie in the annexes on any page. The detection of payment splitting is not a search for the required words in the contract, but a comparison of the schedule, the total amount and the threshold. Moreover, "an identifier address to which a digital depositary provides access" and "a wallet address" are different constructs, although both contain the word "address". Quotations have to be verbatim, and models of a lower class are inclined to paraphrase. |


---

© Никита Мокин / Nikita Mokin  
[GitHub](https://github.com/NikitaMok) · [LinkedIn](https://ru.linkedin.com/in/mokinnikita)

All rights reserved.  
Copying the repository, reproducing substantial parts of the solution and using the code or the product for commercial purposes without the prior written consent of the rights holder are prohibited.  
The publication of the sources on GitHub is intended to demonstrate competence and does not grant a licence for their commercial exploitation.
