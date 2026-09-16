# Limits of use

[Русская версия](LEGAL_DISCLAIMER.md)

The Russian text prevails. This translation is provided for convenience;
statutory citations keep their Russian form because a translated citation is
not a citation.

## This is not a bank opinion and not legal advice

CompLeggeAI produces a **preliminary** analytical report. It must be read and
verified by a human before anything in the contract is changed or any decision
on the deal is taken.

The report:

- is not legal advice and does not replace review by a lawyer;
- does not confirm that the contract complies with the law;
- does not guarantee that a bank will accept the contract, register it, or that
  currency control will end favourably;
- is not evidence of due diligence under Federal Law 115-FZ of 07.08.2001;
- is not to be presented to a bank, a tax authority, a court or a counterparty
  as proof that "the check has been passed".

The decision on the contract is the user's own, at the user's own risk.

## Wallet scoring is not the digital analysis of article 35 of 282-FZ

Article 35 of Federal Law 282-FZ of 04.08.2026 introduces digital analysis —
the examination of digital currencies and identifier addresses with a risk level
assigned to the transaction. Part 3 of that article places the duty to ensure it
on persons organising the circulation of digital currencies, on clearing
organisations and on operators. Engaging third parties is permitted only from
the register of digital-analysis service providers (part 11).

CompLeggeAI **does not provide digital-analysis services** and is not entered in
that register. Wallet scoring is a preliminary assessment based on open
blockchain data.

Counterparty checks rely on open registries. The Russian party is checked
against EGRUL and public company cards; the foreign party against
OpenCorporates and the LEI register (GLEIF). This reflects the state of the
record at the moment of the query, not legal capacity on the settlement date.

## The service does not offer ways around the law

Part 2 of article 30 of Federal Law 282-FZ of 04.08.2026 prohibits assisting
residents in transactions with digital currencies that breach the law,
including informing them of ways to carry out such transactions and supplying
software for that purpose.

CompLeggeAI checks a contract against the requirements and offers wording that
brings it into compliance. It does not produce schemes for circumventing
requirements, ways of staying below mandatory-control thresholds, or techniques
for concealing the true subject matter of a contract. Where a structure is
impermissible, the report says so directly.

## Currency of the legal basis

282-FZ has been in force since 1 September 2026, with certain provisions taking
effect later (article 56). The law refers to acts of the Bank of Russia and of
the Government that are adopted separately. A gap between the appearance of a
subordinate act and its reflection in the rule matrix is unavoidable.

## What leaves the machine and what does not

The text of the uploaded contract is processed on the same machine that runs the
service (local Ollama). It is not sent to any external language model. The file
sits in a temporary directory and is deleted as soon as the report is produced.
The log records size, duration and outcome, never content.

Only extracted identifiers leave for the internet: the address, the INN, the
party name. The contract itself is not part of those requests. If Ollama is
configured on a rented GPU host, the contract text is processed there — that is
your machine, not a public generation service.
