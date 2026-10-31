run "plans_example" {
  command = plan

  assert {
    condition     = terraform_data.example.input == "example"
    error_message = "The example resource must store its fixed input."
  }
}
