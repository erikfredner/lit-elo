# How large language models rank the US literary canon

## Introduction

Empirical studies of literacy canons face a fundamental problem: literary canons are known to consist of a finite number of authors and works, but it is not possible to comprehensively enumerate them. As John Guillory has argued, it is not possible for that list to ever be comprehensively enumerated in any particular time or place because canons are always already contested, and any particular finite canon (say, a new edition of a Norton Anthology) must be studied in relation to both what it includes and what it excludes.

Less often discussed but no less important is that canonicity is not a binary, but a continuum. It is not that Herman Melville is canonical and Hamlin Garland is not. It is that Melville is more canonical than Hamlin Garland, and that Garland is more canonical than James Wellesley Gally. While I imagine that nearly every scholar of nineteenth-century US literature would agree with the hierarchy of canonicity I presented in the previous sentence (assuming they do not adopt a binary view of canonicity), such comparisons are not naturally recurring residua of literary scholarship.

As a consequence of its structural incompleteness, the most fruitful technique for the empirical study of the literary canon has been to identify influential proxies for canonicity, such as literary anthologies and bibliographies, and compare the canons they construct. Large language models (LLMs) offer another possible proxy, which this essay identifies and evaluates.

One of Guillory's points about the structural incompleteness of the canon is that the private judgments of actors in the social systems involved in canonization (e.g., the academy) influence its construction even when they are unrecorded. Syllabi do not indicate that a scholar was torn between teaching two authors and finally chose 

## Method

Beginning with lists of US authors and works seeded from data released from the MLA International Bibliography for another research project, I ask multiple LLMs to adapt the role of a professor of American literatures, then choose which of a given pair of authors or works is more canonical. In certain cases, the results appear to be trivial. Few would dispute that 

## Debates in DH proposal

How do large language models (LLMs) rank the US literary canon? Empirical studies of literary canons have long relied on a variety of proxies for canonicity such as selections for literary anthologies, and recognition in disciplinary resources like the MLA
 International Bibliography (MLAIB). However, the first objection to quantitative analyses of canonicity (besides the canon itself) has always been that any given proxy is inadequate to the problem of canonicity as such. So, the strategy has been to collect
 and compare multiple proxies, none of which can be adequate to the question. LLMs offer yet another proxy with a set of effects that are at once similar to other proxies for the literary canon, but dissimilar in the possible ramifications of their canons.
 Starting with MLAIB data about canonical authors and works of US literature, this project asks many different LLMs to compare the canonicity of pairs of authors and texts, and aggregates their responses in order to understand different LLMs' canons. This essay
 will most closely connect to the area of "generative practices" in the CFP, especially the relationship between the cultural record (anthologies, bibliographies) with synthetic cultural data (LLM approximations thereof). The interesting tension in this case
 is that any given proxy of the canon is always already incomplete and contested, and, in certain respects, LLM rankings can be more responsive to contemporary concerns than aggregations that weight past and present selections equally. (For instance, LLMs correctly
 place Toni Morrison far more highly in the authorial rankings than other models of canonicity.) The essay compares different models' canons, and discusses their effects on the representation of US literature in their weights. A proof-of-concept demo for this
 project is available here: https://fredner.org/lit-elo/
