I'd like to create a personal finances app, and would like you to create a comprehensive plan for building out the application.  Here are the guidelines:

- It should be a web application that I can host locally
- It should utilize Azure Postgres for database / storage;
  - Should authenticate with entra ID
  - Server: jsl6906.postgres.database.azure.com
  - Database: personal_storage
  - Schema: needs creation, 'personal_finances'
  - Files / attachments should also be stored in the database itself, if viable
- It should be packaged so I can deploy it as a docker image to my home server
- It should expect a gemini_key to be provided in env, for use of AI features.  AI models should be used liberally for duplicate processing, OCR like functions, evaluation of uploaded document content, etc.
- Core data is around financial transactions
- The design_docs directory has files that should serve as the foundation for the application UI.

Key features of the application:
- Should allow upload of financial transactions from a spreadsheet
  - User can map uploaded spreadsheet columns to app database
  - Application should review potential duplicates as relating to current database, and walk the user through validating those potential duplicates (removing) or confirming separate
  - Should be able to set 'default' values for fields not present in sheet
- Similarly, should allow upload of financial transactions from an uploaded document
  - System should evaluate document and turn into table of transactions
  - Default values should be evaluated from the source document as well, but can be modified by user
  - rest of spreadsheet upload functionality should exist
- Should be able to evaluate current transactions for potential duplicates
  - Confirmed non-duplicates should be monitored, so as to not flag in later reviews
- Should be able to attach 'statements' to a particular transaction, or set of transactions
  - E.g., attaching a water bill document to the payment transaction
  - Should be able to extract usage values for these attached bills (e.g., kwh used, gallons used, period covered, etc.)
  - Transactions should be tagged so that history of all water bills, for example, can be evaluated and reviewed, along with history of associated usage
  - Should have AI functionality to take uploaded document and suggest assignment, other aspects defined above, approved by user
- Should have a component as a chat interface, with LLM capability to ask questions of the data, with uploaded documents as a component of the chat
- Transactions should have the following key fields (or equivalent): BUT OTHERS MAY BE WARRANTED!
  - transaction date
  - category (with reference data for classifying and grouping categories in a hierarchy)
  - amount
  - account / institution (with ref data details)
  - date added
  - notes
- Should have the ability to set budgets and evaluate against set budgets, for different time periods.
  - SHoudl be able to evaluate single purchases as spread across time periods for budget purposes (e.g., single annual payment spread across months of year for budget)
- Should have a robust set of charts and graphs to evaluate trends in income and expenses
  - Particularly, should have tools to automatically identify out of norm spending, etc.
- SHould be able to set up email notifications for significantly out of norm spending, or budget overspending


I'd like to evaluate if there might also be automated ways to extract this data from bank accounts -- I currently have 'tiller' subscription annually, but also could possibly directly use Plaid / Yodlee if viable to avoid this?


I'd also like to build out a mechanism to automatically backfill my significant list of historical bank statements, excel sheets with transaction lists, bill pdfs, etc., into this framework


Let me know if you have questions about intention or clarifications are required.
