# Rule matrix

[Русская версия](RULES_SPEC.md)

The Russian text prevails. This translation is provided for convenience.
Statutory citations keep their Russian form (282-FZ, art. 1 part 7 cl. 1)
because a translated citation is not a citation. Rule wording quoted from the
matrix is given in English, but the wording actually inserted into a contract is
always the Russian text from `config/rules.yaml`.

The specification of contract checks. A lawyer owns this document; development
implements it without deviation. The machine-readable mirror is
`config/rules.yaml`, which must match this document in codes, severity and
statutory references.

Version 1.3 (31.08.2026). Legal basis: 282-FZ and 283-FZ of 04.08.2026, Bank of
Russia Instruction 181-I of 16.08.2017 (cl. 4.3, cl. 5.1 as amended by
Directive 6819-U).

---

## 1. How a rule is built

| Field | Purpose |
|-------|---------|
| `code` | Stable identifier, unchanged when wording is revised |
| `severity` | `mandatory` — red status on breach; `advisory` — yellow |
| `title` | The requirement in one sentence |
| `norm_refs` | Statutory references down to part and clause |
| `effective_from` | The date from which the norm applies |
| `sanction` | The consequence of a breach for the client |
| `check` | Name of a deterministic check in `app/rules/predicates.py`, or `null` |
| `example_bad` / `example_good` | Wording taken from practice |

A contract is red if at least one `mandatory` rule is breached, yellow if only
`advisory` ones are.

A rule whose `effective_from` lies in the future is still evaluated but appears
in a separate "takes effect later" block rather than as a violation.

### Rules are either checked or referred to a lawyer

A rule is either checked by the program or sent for a lawyer's assessment. There
is no third state, and the report shows which.

`check` names a function registered in `app/rules/predicates.py`. A `null` value
means the rule is designed but cannot be automated yet: `RPT-003` (repatriation
not yet introduced). `ADR-005` is checked from 01.07.2027: if settlement does not
go through a person organising the circulation of digital currencies, the
cross-border basis under art. 30 part 1 cl. 2 of 282-FZ must be stated
explicitly.

Of 32 rules, 31 are automated; of the 17 mandatory ones, all 17 are. `RPT-003`
is the only rule without an automatic check. Until 01.07.2027 `ADR-005` appears
in the "takes effect later" block rather than as a violation. Other rules reach
the report as "requires a lawyer's assessment" only when the predicate cannot
reach a conclusion from the text (as `AML-002` does when no jurisdiction is
named).

Checks are deliberately strict: wording that cannot be read unambiguously counts
as absent. A false positive costs the client one extra paragraph in the
contract; a miss costs a refusal by the bank.

Every check result carries an explanation and, where applicable, the clause
numbers of the contract. An empty explanation on a triggered rule is treated as
a defect and is caught by a test.

### Statutory references are verified, not taken on trust

Every entry in `norm_refs` resolves to the text of the norm in the
`data/curated/norms.jsonl` corpus. The test in `tests/test_norms.py` fails if a
reference finds no text and is not declared a known gap in the corpus. This rules
out both typos in the citation and references to clauses that do not exist.

For clause 2 of article 7 of 115-FZ the corpus holds the consolidated text (as
at 10.06.2026; the sixteenth paragraph as amended by subclause "zh" of clause 5
of article 7 of 283-FZ: business reputation of the special officer in digital
currency exchange organisations, digital depositaries and information system
operators). For Instruction 181-I the corpus holds clauses 4.2, 4.3 and 5.1.

---

## 2. A restriction the product itself must observe

Part 2 of article 30 of 282-FZ prohibits assisting residents in transactions
with digital currencies that breach the law, including:

> "1) informing residents of ways to carry out transactions and operations with
> digital currencies in breach of the requirements of this Federal Law;
> 2) providing residents with the ability to carry out transactions and
> operations with digital currencies in breach of the requirements of this
> Federal Law, including **the provision of software** for such transactions
> and operations."

The service is software that advises residents on digital-currency transactions.
Two hard constraints follow.

1. **The system never offers a way around a requirement.** Where a clause does
   not meet a norm, it returns the correct wording, or a statement that the deal
   is impermissible in that structure. Formulations of the "so the bank does not
   notice" kind are excluded at the level of prompts and rules.
2. **Remediation is worded only towards compliance.** Circumvention, splitting
   payments to stay below a threshold, disguising the subject matter of the
   contract — never produced, whatever the user asks. This is a separate
   guardrail check before the report is released.

The restriction is implemented in `app/rules/guardrail.py`. A report does not
leave the engine unchecked: every text that enters it is run against a list of
prohibited constructions. A hit means a defect in the rule matrix rather than a
problem with the contract under review, so the check fails loudly and no report
is issued at all.

The check distinguishes advice from description. Rule `THR-003` must name the
signs of artificial payment splitting — that is legitimate lawyer's wording.
What is prohibited is the imperative: "split the payment", "so the bank does not
notice", "circumvent the requirement", "do not state it in the contract",
"a sham contract", "understate the amount". A separate test runs every text in
the matrix through the check: the product's own wording must pass.

Remediation wording is taken from the `example_good` field of the same rule and
is never composed. A test ensures that no wording absent from the matrix can
appear in a report.

---

## 3. Effective dates that bear on the rules

| Norm | Date |
|------|------|
| The main body of 282-FZ and 283-FZ | 01.09.2026 |
| 282-FZ, art. 1 part 3 and art. 30 part 1 | 01.07.2027 |
| 283-FZ, subcl. "a" cl. 8 art. 9 (86-FZ) | 01.03.2027 |
| 283-FZ, paras 4–9 cl. 8 art. 12 (art. 12.1 of 173-FZ) | 02.05.2027 |
| 283-FZ, cl. 3 art. 16 (161-FZ) | 01.09.2027 |
| 283-FZ, cl. 2 art. 16 (161-FZ) | 01.09.2028 |

The practical consequence: the requirement of part 1 of article 30 — to carry
out digital-currency transactions only through persons organising their
circulation — applies only from 1 July 2027. Until then the regime for an
importer is materially freer, and `ADR-005` is not treated as a violation before
01.07.2027.

---

## 4. Group A. Qualification of the contract

This is the basis on which the whole regime applies. If the contract does not
qualify as cross-border trade between a resident and a non-resident, settlement
in digital currency falls under the general prohibition of part 6 of article 1,
and the remaining checks are moot.

The mechanism that makes this group practical rather than theoretical: a digital
depositary may credit digital currency to a resident depositor's digital account
under clause 6 of part 5 of article 31 only where the currency was acquired to
pay under a cross-border trade contract. If the contract does not say so, the
depositary has nothing to rely on and will refuse the credit.

| Code | Severity | Requirement | Norms |
|------|----------|-------------|-------|
| `FTC-001` | mandatory | The contract is expressly qualified as cross-border trade between a resident and a non-resident | 282-FZ art. 1 part 7 cl. 1; art. 31 part 5 cl. 6 |
| `FTC-002` | mandatory | The subject matter is the transfer of goods, information or intellectual property, the performance of work or the provision of services | 282-FZ art. 1 part 7 cl. 1 |
| `FTC-003` | mandatory | Each party's residency is determinable from the text: name, state of registration, registration number, address | 282-FZ art. 1 part 7 cl. 1 |
| `FTC-004` | advisory | Where a party acts as agent, commission agent or attorney, the contract states on whose behalf and under which contract | 282-FZ art. 1 part 8 |
| `FTC-005` | advisory | The contract cites cl. 1 part 7 art. 1 of 282-FZ as the basis for settlement in digital currency | 282-FZ art. 1 part 7 cl. 1 |

**`FTC-001`.** Sanction: settlement falls under the prohibition of part 6 of
art. 1; the depositary refuses the credit; the bank refuses registration.

Breach: "The Buyer pays for the Goods in USDT."
Remediation: a clause qualifying the contract as cross-border trade between a
resident buyer and a non-resident supplier, providing for the transfer of goods,
with settlement in digital currency on the basis of clause 1 of part 7 of
article 1 of Federal Law 282-FZ of 04.08.2026.

---

## 5. Group B. The asset

| Code | Severity | Requirement | Norms |
|------|----------|-------------|-------|
| `AST-001` | mandatory | The digital currency is identified unambiguously: name, ticker, network and token standard, issuer | 282-FZ art. 2 part 1 cl. 1 |
| `AST-002` | mandatory | The contract states that the digital currency serves as means of payment or consideration under this very contract | 282-FZ art. 1 part 7 cl. 1; art. 31 part 5 cl. 6 |
| `AST-003` | advisory | The consequences of a stablecoin losing its peg, and of delisting, are addressed | — (contract practice) |
| `AST-004` | advisory | Where the asset is a foreign digital instrument, the contract reflects that digital-currency rules apply to it | 282-FZ art. 1 part 4; 115-FZ art. 3 parts 7–8 |

**`AST-001`.** Sanction: the object of payment cannot be identified; currency
control objects; risk of the operation being recharacterised.

Breach: "Payment is made in stablecoins."
Remediation: payment in the digital currency Tether USD (ticker USDT), issuer
Tether Limited, on the TRON network, token standard TRC-20.

---

## 6. Group C. Addresses and settlement

| Code | Severity | Requirement | Norms |
|------|----------|-------------|-------|
| `ADR-001` | mandatory | The payee address is stated as an identifier address made accessible by a digital depositary, not as a raw wallet address | 282-FZ art. 24 parts 1–2; art. 17 |
| `ADR-002` | mandatory | The digital depositary is named with register data allowing its licence to be verified | 282-FZ art. 17; art. 52 |
| `ADR-003` | advisory | A right to demand a depositary statement as proof of holding is provided | 282-FZ art. 24 part 4 |
| `ADR-004` | advisory | Where a non-depositary identifier address is used, the duty to report to the tax authorities is addressed | 173-FZ art. 12.1 |
| `ADR-005` | mandatory from 01.07.2027 | Where settlement does not go through a person organising the circulation of digital currencies, the cross-border basis is stated expressly | 282-FZ art. 30 part 1 cl. 2 |

**`ADR-001`.** Sanction: blocking under 115-FZ; the bank refuses registration;
holding of the asset cannot be evidenced.

Breach: "Payment is made to wallet `TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE`."
Remediation: payment to an identifier address made accessible by a named digital
depositary entered in the Bank of Russia register (with its register number),
the address to be set out in a supplementary agreement and confirmed by a
depositary statement.

---

## 7. Group D. Exchange rate and the moment of performance

| Code | Severity | Requirement | Norms |
|------|----------|-------------|-------|
| `RTE-001` | mandatory | The method of determining the rate is fixed: the quotation source and the moment of fixing | 282-FZ art. 34 |
| `RTE-002` | mandatory | The moment of performance of the monetary obligation is tied to the entry of a record in the information system in which the digital currency is recorded | 282-FZ art. 30 part 3 |
| `RTE-003` | advisory | The allocation of information-system fees is addressed | 282-FZ art. 1 part 7 cl. 3 |
| `RTE-004` | advisory | A permissible rate deviation and the procedure for topping up or refunding the difference are set | — (contract practice) |

**`RTE-002`.** Part 3 of article 30 states directly that an operation is
performed from the moment a record is entered in the information system. A
contract that ties performance to "the moment the transfer is sent" diverges from
the law and creates a dispute over whether the obligation was discharged when a
transaction hangs.

Breach: "The payment obligation is deemed performed from the moment the Buyer
sends the transfer."
Remediation: performance from the moment the record crediting the digital
currency to the Supplier's identifier address is entered in the information
system in which that digital currency is recorded (part 3 of article 30 of
Federal Law 282-FZ of 04.08.2026).

---

## 8. Group E. Party details

Article 7.2-1 of 115-FZ requires an operation to be accompanied by details of
the payer and the payee. Without them a digital depositary **must refuse** to
execute the instruction, and the payee's depositary may reject the operation.
This is a technical obstacle: a contract lacking the full set of details will not
be performed however good its other terms.

| Code | Severity | Requirement | Norms |
|------|----------|-------------|-------|
| `TRV-001` | mandatory | For a legal-entity party: name, digital account or identifier address number, state and city of location, INN | 115-FZ art. 7.2-1 cl. 1 subcl. 2 and 4 |
| `TRV-002` | mandatory | Where the operation exceeds RUB 60,000, the details match the extended list | 115-FZ art. 7.2-1 cl. 1 |
| `TRV-003` | advisory | A duty to keep details current, and the consequences of failing to supply them, are set | 115-FZ art. 7.2-1 cl. 5 and 9 |

---

## 9. Group F. Thresholds and control

| Code | Severity | Requirement | Norms |
|------|----------|-------------|-------|
| `THR-001` | mandatory | Where the operation is RUB 10,000,000 or more, the contract carries terms enabling mandatory control | 115-FZ art. 6 cl. 1.12 |
| `THR-002` | mandatory | Where an import contract is RUB 3,000,000 or more, registration with an authorised bank is provided for | 181-I cl. 4.3; cl. 5.1 |
| `THR-003` | advisory | There are no signs of artificial payment splitting to stay below a threshold | 115-FZ art. 6 cl. 1.12; art. 7 cl. 2 |

The thresholds are independent and are reported separately. They must not be
conflated: RUB 10 million is mandatory control by Rosfinmonitoring under 115-FZ;
RUB 3 million is registration of an import contract under clause 4.3 of
Instruction 181-I. For an export contract the same clause sets RUB 10 million.
The product is designed for an importer; where the text unambiguously indicates
export, the RUB 3 million threshold must not be applied — that is a separate
assessment by a lawyer.

Clause 4.3 as amended by Directive 6819-U covers ordinary import contracts and
"DR contracts" (where digital rights are the means of payment). The Instruction
does not name settlement in digital currency in those clauses. The Bank of Russia
may set a procedure for documents on digital-currency operations under part 18 of
article 23 of 173-FZ. Until a separate procedure takes effect, the threshold of
clause 4.3 applies to a cross-border import contract as an object of
registration.

`THR-002`. Breach: the amount is RUB 3 million or more and registration is not
addressed in the contract.
Remediation: the Buyer undertakes to register the contract with an authorised
bank in accordance with Bank of Russia Instruction 181-I of 16.08.2017 before
the first payment.

`THR-003` looks for a payment schedule in which each tranche falls just below the
threshold while the total exceeds it. The rule reports the risk and does not
suggest how to exploit it (see section 2).

---

## 10. Group G. Risk and AML

| Code | Severity | Requirement | Norms |
|------|----------|-------------|-------|
| `AML-001` | mandatory | Risks of freezing or rejection of the operation are allocated, including by a foreign counterparty and a foreign financial-market organisation | 282-FZ art. 35; 115-FZ art. 7.2-1 cl. 9 |
| `AML-002` | mandatory | The counterparty's identifier address is not administered by an organisation from a state that does not implement FATF recommendations, or this is expressly addressed | 115-FZ art. 6 cl. 1 subcl. 2 |
| `AML-003` | advisory | A right to suspend performance on signs of elevated risk is provided | 282-FZ art. 35 |
| `AML-004` | advisory | The counterparty represents as to the origin of the digital currency and the absence of any link to unlawful activity | 282-FZ art. 35 part 1 |

**`AML-002`.** Subclause 2 of clause 1 of article 6 of 115-FZ as amended: an
operation is subject to mandatory control **regardless of amount** if it uses an
identifier address administered by a financial-market organisation from a state
that does not implement FATF recommendations. What is checked is the venue's
jurisdiction, not only the address history.

The list of such states is determined in a procedure established by the
Government, taking FATF documents into account, and is to be published. As at
01.09.2026 no new Government list has been found. `config/fatf_jurisdictions.yaml`
holds two layers: a snapshot of the FATF statement of 19.06.2026 (Iran, DPRK,
Myanmar) and a marker of whether a territory appears in Rosfinmonitoring Order
361 of 10.11.2011 (Iran and DPRK, without Myanmar). The snapshot is not
substituted by the Order. If the contract names a state from the snapshot and
contains no clause on mandatory control regardless of amount, the rule is
breached. If the clause is present, it is met. If the venue's jurisdiction cannot
be read from the text, the rule stays with the lawyer and does not colour the
status. This is not the official Government list.

---

## 11. Group H. Documents and reporting

| Code | Severity | Requirement | Norms |
|------|----------|-------------|-------|
| `RPT-001` | mandatory | A duty to supply supporting documents on digital-currency operations is set | 173-FZ art. 23 part 18 |
| `RPT-002` | advisory | Documents on operations are retained for at least five years | 282-FZ art. 23 part 11; art. 39 |
| `RPT-003` | advisory | The possible introduction of a digital-currency repatriation requirement is addressed | 173-FZ art. 19 part 9 |
| `RPT-004` | advisory | Where settlement uses a non-depositary address, assistance in preparing the tax report is provided for | 173-FZ art. 12.1 part 2 |

`RPT-003` is a deferred rule. No repatriation requirement was in force as at
28.08.2026; the rule sits in the matrix inactive and activates when a Government
resolution appears.

---

## 12. Summary

| Group | Rules | Of which mandatory |
|-------|-------|--------------------|
| A. Qualification of the contract | 5 | 3 |
| B. The asset | 4 | 2 |
| C. Addresses and settlement | 5 | 3 |
| D. Rate and moment of performance | 4 | 2 |
| E. Party details | 3 | 2 |
| F. Thresholds and control | 3 | 2 |
| G. Risk and AML | 4 | 2 |
| H. Documents and reporting | 4 | 1 |
| **Total** | **32** | **17** |

---

## 13. Open points to verify

1. The registration threshold under Instruction 181-I has been reconciled with
   clauses 4.3 and 5.1 as amended by Bank of Russia Directive 6819-U of
   06.08.2024: import RUB 3 million, export RUB 10 million. A "DR contract"
   means digital rights, not digital currency.
2. Paragraphs four to nine of clause 8 of article 12 of 283-FZ (art. 12.1 of
   173-FZ) take effect 270 days after publication (02.05.2027). Counting the
   paragraphs of that clause, parts 2–7 of article 12.1 are deferred; part 1
   applies from 01.09.2026. `ADR-004` and `RPT-004` remain advisory from
   01.09.2026: part 2 may be among the deferred paragraphs, and the Government
   has not yet approved the reporting procedure.
3. Whether a Government resolution on the procedure for reporting
   digital-currency operations exists.
4. The composition and availability of Bank of Russia registers of digital
   depositaries (`ADR-002`). As at 01.09.2026 the Bank of Russia has published an
   admission navigator (https://cbr.ru/admissionfinmarket/navigator/cd/, updated
   28.08.2026). The register page https://www.cbr.ru/registries/ was updated on
   01.09.2026 and carries no list of digital depositaries. Bank of Russia
   Regulation 890-P of 27.08.2026 and Directive 7429-U of 27.08.2026 are with the
   Ministry of Justice for state registration
   (https://cbr.ru/admissionfinmarket/acts/). Applications under the "4010
   Applicants" procedure open on the date 890-P takes effect. Participants in the
   experimental legal regime may file until 01.09.2027. A run performs no
   reconciliation against a register, and the explanation says so plainly.
5. The list of states that do not implement FATF recommendations: no Government
   list under 283-FZ had been published as at 01.09.2026. The service holds a
   snapshot of the FATF statement of 19.06.2026
   (`config/fatf_jurisdictions.yaml`: Iran, DPRK, Myanmar) and, separately,
   Rosfinmonitoring Order 361 of 10.11.2011 (Iran and DPRK, without Myanmar).
   The snapshot is not substituted by the Order.
